import asyncio
import copy
from typing import Optional, Union, Callable, List, Dict

import aiohttp

from .dtypes import _termination_obj, Event, FlowError, V3ioError
from .table import Table
from .utils import _split_path


class Flow:
    def __init__(self, name=None, full_event=False, termination_result_fn=lambda x, y: x if x is not None else y, context=None, **kwargs):
        self._outlets = []
        self._full_event = full_event
        self._termination_result_fn = termination_result_fn
        self.context = context
        self._closeables = []
        if name:
            self.name = name
        else:
            self.name = type(self).__name__

    def to(self, outlet):
        self._outlets.append(outlet)
        return outlet

    def run(self):
        for outlet in self._outlets:
            self._closeables.extend(outlet.run())
        return self._closeables

    async def run_async(self):
        raise NotImplementedError

    async def _do(self, event):
        raise NotImplementedError

    async def _do_downstream(self, event):
        if not self._outlets:
            return
        if event is _termination_obj:
            termination_result = await self._outlets[0]._do(_termination_obj)
            for i in range(1, len(self._outlets)):
                termination_result = self._termination_result_fn(termination_result, await self._outlets[i]._do(_termination_obj))
            return termination_result
        # If there is more than one outlet, allow concurrent execution.
        tasks = []
        for i in range(1, len(self._outlets)):
            tasks.append(asyncio.get_running_loop().create_task(self._outlets[i]._do(event)))
        await self._outlets[0]._do(event)  # Optimization - avoids creating a task for the first outlet.
        for task in tasks:
            await task

    def _get_safe_event_or_body(self, event):
        if self._full_event:
            new_event = copy.copy(event)
            return new_event
        else:
            return event.body

    def _user_fn_output_to_event(self, event, fn_result):
        if self._full_event:
            return fn_result
        else:
            mapped_event = copy.copy(event)
            mapped_event.body = fn_result
            return mapped_event


class Choice(Flow):
    """Redirects each input element into at most one of multiple downstreams.

    :param choice_array: a list of (downstream, condition) tuples, where downstream is a step and condition is a function. The first
    condition in the list to evaluate as true for an input element causes that element to be redirected to that downstream step.
    :type choice_array: tuple of (Flow, Function (Event=>boolean))

    :param default: a default step for events that did not match any condition in choice_array. If not set, elements that don't match any
    condition will be discarded.
    :type default: Flow
    :param name: Name of this step, as it should appear in logs. Defaults to class name (Choice).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    def __init__(self, choice_array, default=None, **kwargs):
        Flow.__init__(self, **kwargs)

        self._choice_array = choice_array
        for outlet, _ in choice_array:
            self._outlets.append(outlet)

        if default:
            self._outlets.append(default)
        self._default = default

    async def _do(self, event):
        if not self._outlets or event is _termination_obj:
            return await super()._do_downstream(event)
        chosen_outlet = None
        element = self._get_safe_event_or_body(event)
        for outlet, condition in self._choice_array:
            if condition(element):
                chosen_outlet = outlet
                break
        if chosen_outlet:
            await chosen_outlet._do(event)
        elif self._default:
            await self._default._do(event)


class _UnaryFunctionFlow(Flow):
    def __init__(self, fn, **kwargs):
        super().__init__(**kwargs)
        if not callable(fn):
            raise TypeError(f'Expected a callable, got {type(fn)}')
        self._is_async = asyncio.iscoroutinefunction(fn)
        self._fn = fn

    async def _call(self, element):
        res = self._fn(element)
        if self._is_async:
            res = await res
        return res

    async def _do_internal(self, element, fn_result):
        raise NotImplementedError()

    async def _do(self, event):
        if event is _termination_obj:
            return await self._do_downstream(_termination_obj)
        else:
            element = self._get_safe_event_or_body(event)
            fn_result = await self._call(element)
            await self._do_internal(event, fn_result)


class Map(_UnaryFunctionFlow):
    """Maps, or transforms, incoming events using a user-provided function.

    :param fn: Function to apply to each event
    :type fn: Function (Event=>Event)
    :param name: Name of this step, as it should appear in logs. Defaults to class name (Map).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    async def _do_internal(self, event, fn_result):
        mapped_event = self._user_fn_output_to_event(event, fn_result)
        await self._do_downstream(mapped_event)


class Filter(_UnaryFunctionFlow):
    """Filters events based on a user-provided function.

    :param fn: Function to decide whether to keep each event.
    :type fn: Function (Event=>boolean)
    :param name: Name of this step, as it should appear in logs. Defaults to class name (Filter).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    async def _do_internal(self, event, keep):
        if keep:
            await self._do_downstream(event)


class FlatMap(_UnaryFunctionFlow):
    """Maps, or transforms, each incoming event into any number of events.

    :param fn: Function to transform each event to a list of events.
    :type fn: Function (Event=>list of Event)
    :param name: Name of this step, as it should appear in logs. Defaults to class name (FlatMap).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    async def _do_internal(self, event, fn_result):
        for fn_result_element in fn_result:
            mapped_event = self._user_fn_output_to_event(event, fn_result_element)
            await self._do_downstream(mapped_event)


class Extend(_UnaryFunctionFlow):
    async def _do_internal(self, event, fn_result):
        for key, value in fn_result.items():
            event.body[key] = value
        await self._do_downstream(event)


class _FunctionWithStateFlow(Flow):
    def __init__(self, initial_state, fn, group_by_key=False, **kwargs):
        super().__init__(**kwargs)
        if not callable(fn):
            raise TypeError(f'Expected a callable, got {type(fn)}')
        self._is_async = asyncio.iscoroutinefunction(fn)
        self._state = initial_state
        self._fn = fn
        self._group_by_key = group_by_key
        if hasattr(initial_state, 'close'):
            self._closeables = [initial_state]

    async def _call(self, event):
        element = self._get_safe_event_or_body(event)
        if self._group_by_key:
            if isinstance(self._state, Table):
                key_data = await self._state.get_or_load_key(event.key)
            else:
                key_data = self._state[event.key]
            res, self._state[event.key] = self._fn(element, key_data)
        else:
            res, self._state = self._fn(element, self._state)
        if self._is_async:
            res = await res
        return res

    async def _do_internal(self, element, fn_result):
        raise NotImplementedError()

    async def _do(self, event):
        if event is _termination_obj:
            return await self._do_downstream(_termination_obj)
        else:

            fn_result = await self._call(event)
            await self._do_internal(event, fn_result)


class MapWithState(_FunctionWithStateFlow):
    """Maps, or transforms, incoming events using a stateful user-provided function, and an initial state, which may be a database table.

    :param initial_state: Initial state for the computation. If group_by_key is True, this must be a dictionary or a Table object.
    :type initial_state: dictionary or Table if group_by_key is True. Any object otherwise.
    :param fn: A function to run on each event and the current state. Must yield an event and an updated state.
    :type fn: Function ((Event, state)=>(Event, state))
    :param group_by_key: Whether the state is computed by key. Optional. Default to False.
    :type group_by_key: boolean
    :param full_event: Whether fn will receive and return an Event object or only the body (payload). Optional. Defaults to
    False (body only).
    :type full_event: boolean
    """

    async def _do_internal(self, event, mapped_element):
        mapped_event = self._user_fn_output_to_event(event, mapped_element)
        await self._do_downstream(mapped_event)


class MapClass(Flow):
    """Similar to Map, but instead of a function argument, this class should be extended and its do() method overridden."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._is_async = asyncio.iscoroutinefunction(self.do)
        self._filter = False

    def filter(self):
        # used in the .do() code to signal filtering
        self._filter = True

    def do(self, event):
        raise NotImplementedError()

    async def _call(self, event):
        res = self.do(event)
        if self._is_async:
            res = await res
        return res

    async def _do(self, event):
        if event is _termination_obj:
            return await self._do_downstream(_termination_obj)
        else:
            element = self._get_safe_event_or_body(event)
            fn_result = await self._call(element)
            if not self._filter:
                mapped_event = self._user_fn_output_to_event(event, fn_result)
                await self._do_downstream(mapped_event)
            else:
                self._filter = False  # clear the flag for future runs


class Complete(Flow):
    """
        Completes the AwaitableResult associated with incoming events.
        :param name: Name of this step, as it should appear in logs. Defaults to class name (Complete).
        :type name: string
        :param full_event: Whether to complete with an Event object (when True) or only the payload (when False). Default to False.
        :type full_event: boolean
    """

    async def _do(self, event):
        termination_result = await self._do_downstream(event)
        if event is not _termination_obj:
            result = self._get_safe_event_or_body(event)
            res = event._awaitable_result._set_result(result)
            if res:
                await res
        return termination_result


class Reduce(Flow):
    """
        Reduces incoming events into a single value which is returned upon the successful termination of the flow.
        :param initial_value: Starting value. When the first event is received, fn will be appled to the initial_value and that event.
        :type initial_value: object
        :param fn: Function to apply to the current value and each event.
        :type fn: Function ((object, Event) => object)
        :param name: Name of this step, as it should appear in logs. Defaults to class name (Reduce).
        :type name: string
        :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
        Defaults to False.
        :type full_event: boolean
    """

    def __init__(self, initial_value, fn, **kwargs):
        super().__init__(**kwargs)
        if not callable(fn):
            raise TypeError(f'Expected a callable, got {type(fn)}')
        self._is_async = asyncio.iscoroutinefunction(fn)
        self._fn = fn
        self._result = initial_value

    def to(self, outlet):
        raise ValueError("Reduce is a terminal step. It cannot be piped further.")

    async def _do(self, event):
        if event is _termination_obj:
            return self._result
        else:
            if self._full_event:
                elem = event
            else:
                elem = event.body
            res = self._fn(self._result, elem)
            if self._is_async:
                res = await res
            self._result = res


class HttpRequest:
    """A class representing an HTTP request, with method, url, body, and headers.

    :param method: HTTP method (e.g. GET).
    :type method: string
    :param url: Target URL (http and https schemes supported).
    :type url: string
    :param body: Request body.
    :type body: bytes or string
    :param headers: Request headers, in the form of a dictionary. Optional. Defaults to no headers.
    :type headers: dictionary, or None.
    """

    def __init__(self, method, url, body, headers: Optional[dict] = None):
        self.method = method
        self.url = url
        self.body = body
        if headers is None:
            headers = {}
        self.headers = headers


class HttpResponse:
    """A class representing an HTTP response, with a status code and body.

    :param body: Response body.
    :type body: bytes
    :param status: HTTP status code.
    :type status: int
    """

    def __init__(self, status, body):
        self.status = status
        self.body = body


class _ConcurrentJobExecution(Flow):
    def __init__(self, max_in_flight=8, **kwargs):
        Flow.__init__(self, **kwargs)
        self._max_in_flight = max_in_flight
        self._q = None

    async def _worker(self):
        try:
            while True:
                job = await self._q.get()
                if job is _termination_obj:
                    break
                event = job[0]
                completed = await job[1]
                await self._handle_completed(event, completed)
        except BaseException as ex:
            if not self._q.empty():
                await self._q.get()
            raise ex
        finally:
            await self._cleanup()

    async def _process_event(self, event):
        raise NotImplementedError()

    async def _handle_completed(self, event, response):
        raise NotImplementedError()

    async def _cleanup(self):
        pass

    async def _lazy_init(self):
        pass

    async def _do(self, event):
        if not self._q:
            await self._lazy_init()
            self._q = asyncio.queues.Queue(self._max_in_flight)
            self._worker_awaitable = asyncio.get_running_loop().create_task(self._worker())

        if self._worker_awaitable.done():
            await self._worker_awaitable
            raise FlowError("ConcurrentJobExecution worker has already terminated")

        if event is _termination_obj:
            await self._q.put(_termination_obj)
            await self._worker_awaitable
            return await self._do_downstream(_termination_obj)
        else:
            task = self._process_event(event)
            await self._q.put((event, asyncio.get_running_loop().create_task(task)))
            if self._worker_awaitable.done():
                await self._worker_awaitable


class _PendingEvent:
    def __init__(self):
        self.in_flight = []
        self.pending = []


class _ConcurrentByKeyJobExecution(Flow):
    def __init__(self, max_in_flight=8, **kwargs):
        Flow.__init__(self, **kwargs)
        self._max_in_flight = max_in_flight
        self._q = None
        self._pending_by_key = {}

    async def _worker(self):
        try:
            while True:
                job = await self._q.get()
                if job is _termination_obj:
                    for pending_event in self._pending_by_key.values():
                        if pending_event.pending and not pending_event.in_flight:
                            resp = await self._process_event(pending_event.pending)
                            for event in pending_event.pending:
                                await self._handle_completed(event, resp)
                    if self._q.empty():
                        break
                    else:
                        await self._q.put(_termination_obj)
                        continue

                event = job[0]
                completed = await job[1]

                for event in self._pending_by_key[event.key].in_flight:
                    await self._handle_completed(event, completed)
                self._pending_by_key[event.key].in_flight = []

                # If we got more pending events for the same key process them
                if self._pending_by_key[event.key].pending:
                    self._pending_by_key[event.key].in_flight = self._pending_by_key[event.key].pending
                    self._pending_by_key[event.key].pending = []

                    task = self._process_event(self._pending_by_key[event.key].in_flight)
                    await self._q.put((event, asyncio.get_running_loop().create_task(task)))
                else:
                    del self._pending_by_key[event.key]
        except BaseException as ex:
            if not self._q.empty():
                await self._q.get()
            raise ex
        finally:
            await self._cleanup()

    async def _do(self, event):
        if not self._q:
            await self._lazy_init()
            self._q = asyncio.queues.Queue(self._max_in_flight)
            self._worker_awaitable = asyncio.get_running_loop().create_task(self._worker())

        if self._worker_awaitable.done():
            await self._worker_awaitable
            raise FlowError("ConcurrentByKeyJobExecution worker has already terminated")

        if event is _termination_obj:
            await self._q.put(_termination_obj)
            await self._worker_awaitable
            return await self._do_downstream(_termination_obj)
        else:
            # Initializing the key with 2 lists. One for pending requests and one for requests that an update request has been issued for.
            if event.key not in self._pending_by_key:
                self._pending_by_key[event.key] = _PendingEvent()

            # If there is a current update in flight for the key, add the event to the pending list. Otherwise update the key.
            self._pending_by_key[event.key].pending.append(event)
            if len(self._pending_by_key[event.key].in_flight) == 0:
                self._pending_by_key[event.key].in_flight = self._pending_by_key[event.key].pending
                self._pending_by_key[event.key].pending = []

                task = self._process_event(self._pending_by_key[event.key].in_flight)
                await self._q.put((event, asyncio.get_running_loop().create_task(task)))
                if self._worker_awaitable.done():
                    await self._worker_awaitable

    async def _process_event(self, event):
        raise NotImplementedError()

    async def _handle_completed(self, event, response):
        raise NotImplementedError()

    async def _cleanup(self):
        pass

    async def _lazy_init(self):
        pass


class SendToHttp(_ConcurrentJobExecution):
    """Joins each event with data from any HTTP source. Used for event augmentation.

    :param request_builder: Creates an HTTP request from the event. This request is then sent to its destination.
    :type request_builder: Function (Event=>HttpRequest)
    :param join_from_response: Joins the original event with the HTTP response into a new event.
    :type join_from_response: Function ((Event, HttpResponse)=>Event)
    :param name: Name of this step, as it should appear in logs. Defaults to class name (SendToHttp).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    def __init__(self, request_builder, join_from_response, **kwargs):
        super().__init__(**kwargs)
        self._request_builder = request_builder
        self._join_from_response = join_from_response

        self._client_session = None

    async def _lazy_init(self):
        connector = aiohttp.TCPConnector()
        self._client_session = aiohttp.ClientSession(connector=connector)

    async def _cleanup(self):
        await self._client_session.close()

    async def _process_event(self, event):
        req = self._request_builder(event)
        return await self._client_session.request(req.method, req.url, headers=req.headers, data=req.body, ssl=False)

    async def _handle_completed(self, event, response):
        response_body = await response.text()
        joined_element = self._join_from_response(event.body, HttpResponse(response.status, response_body))
        if joined_element is not None:
            new_event = self._user_fn_output_to_event(event, joined_element)
            await self._do_downstream(new_event)


class _Batching(Flow):
    def __init__(self, max_events: Optional[int] = None, timeout_secs=None, **kwargs):
        super().__init__(**kwargs)

        self._max_events = max_events
        self._event_count = 0
        self._batch = []
        self._batch_time = None
        self._timeout_task = None

        self._timeout_secs = timeout_secs
        if self._timeout_secs is not None and self._timeout_secs <= 0:
            raise ValueError('Batch timeout cannot be 0 or negative')

    async def _emit(self, batch, batch_time):
        raise NotImplementedError

    async def _terminate(self):
        pass

    async def _sleep_and_emit(self):
        await asyncio.sleep(self._timeout_secs)
        await self._emit_batch()

    def _event_to_batch_entry(self, event):
        return self._get_safe_event_or_body(event)

    async def _do(self, event):
        if event is _termination_obj:
            if self._timeout_task and not self._timeout_task.cancelled():
                self._timeout_task.cancel()
            await self._emit_batch()
            await self._terminate()
            return await self._do_downstream(_termination_obj)
        else:
            if len(self._batch) == 0:
                self._batch_time = event.time
                if self._timeout_secs:
                    self._timeout_task = asyncio.get_running_loop().create_task(self._sleep_and_emit())

            self._event_count = self._event_count + 1
            self._batch.append(self._event_to_batch_entry(event))

            if self._event_count == self._max_events:
                if self._timeout_task and not self._timeout_task.cancelled():
                    self._timeout_task.cancel()
                await self._emit_batch()

    async def _emit_batch(self):
        if len(self._batch) > 0:
            batch_to_emit = self._batch
            batch_time = self._batch_time
            self._batch = []
            self._batch_time = None
            self._event_count = 0

            await self._emit(batch_to_emit, batch_time)


class Batch(_Batching):
    """
    Batches events into lists of up to max_events events. Each emitted list contained max_events events, unless timeout_secs seconds
    have passed since the first event in the batch was received, at which the batch is emitted with potentially fewer than max_events
    event.
    :param max_events: Maximum number of events per emitted batch. Set to None to emit all events in one batch on flow termination.
    :type max_events: int or None
    :param timeout_secs: Maximum number of seconds to wait before a batch is emitted.
    :type timeout_secs: int
    """

    async def _emit(self, batch, batch_time):
        event = Event(batch, time=batch_time)
        return await self._do_downstream(event)


class JoinWithV3IOTable(_ConcurrentJobExecution):
    """Joins each event with a V3IO table. Used for event augmentation.

    :param storage: V3IO driver.
    :type storage: V3ioDriver
    :param key_extractor: Function for extracting the key for table access from an event.
    :type key_extractor: Function (Event=>string)
    :param join_function: Joins the original event with relevant data received from V3IO.
    :type join_function: Function ((Event, dict)=>Event)
    :param table_path: Path to the table in V3IO.
    :type table_path: string
    :param attributes: A comma-separated list of attributes to be requested from V3IO. Defaults to '*' (all user attributes).
    :type attributes: string
    :param name: Name of this step, as it should appear in logs. Defaults to class name (JoinWithV3IOTable).
    :type name: string
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :type full_event: boolean
    """

    def __init__(self, storage, key_extractor, join_function, table_path, attributes='*', **kwargs):
        super().__init__(**kwargs)

        self._storage = storage

        self._key_extractor = key_extractor
        self._join_function = join_function

        self._container, self._table_path = _split_path(table_path)
        self._attributes = attributes

    async def _process_event(self, event):
        key = str(self._key_extractor(self._get_safe_event_or_body(event)))
        return await self._storage._get_item(self._container, self._table_path, key, self._attributes)

    async def _handle_completed(self, event, response):
        if response.status_code == 200:
            response_object = response.output.item
            joined = self._join_function(self._get_safe_event_or_body(event), response_object)
            if joined is not None:
                new_event = self._user_fn_output_to_event(event, joined)
                await self._do_downstream(new_event)
        elif response.status_code == 404:
            return None
        else:
            raise V3ioError(f'Failed to get item. Response status code was {response.status_code}: {response.body}')

    async def _cleanup(self):
        await self._storage.close()


class JoinWithTable(_ConcurrentJobExecution):
    """Joins each event with data from the given table.

    :param table: A Table object or name to join with. If a table name is provided, it will be looked up in the context.
    :param key_extractor: Key's column name or a function for extracting the key, for table access from an event.
    :param attributes: A comma-separated list of attributes to be queried for. Defaults to all attributes.
    :param join_function: Joins the original event with relevant data received from the storage. Defaults to assume the event's body is a
    dict-like object and updating it.
    :param name: Name of this step, as it should appear in logs. Defaults to class name (JoinWithTable).
    :param full_event: Whether user functions should receive and/or return Event objects (when True), or only the payload (when False).
    Defaults to False.
    :param context: Context object that holds global configurations and secrets.
    """

    def __init__(self, table: Union[Table, str], key_extractor: Union[str, Callable[[Event], str]], attributes: Optional[List[str]] = None,
                 join_function: Optional[Callable[[Event, Dict[str, object]], Event]] = None, **kwargs):
        super().__init__(**kwargs)

        self._table = table
        if isinstance(table, str):
            if not self.context:
                raise TypeError("Table can not be string if no context was provided to the step")
            self._table = self.context.get_table(table)
        self._closeables = [self._table]

        if key_extractor:
            if callable(key_extractor):
                self._key_extractor = key_extractor
            elif isinstance(key_extractor, str):
                self._key_extractor = lambda element: element[key_extractor]
            else:
                raise TypeError(f'key is expected to be either a callable or string but got {type(key_extractor)}')

        def default_join_fn(event, join_res):
            event.update(join_res)
            return event

        self._join_function = join_function or default_join_fn

        self._attributes = attributes or '*'

    async def _process_event(self, event):
        key = self._key_extractor(self._get_safe_event_or_body(event))
        return await self._table.get_or_load_key(key, self._attributes)

    async def _handle_completed(self, event, response):
        joined = self._join_function(self._get_safe_event_or_body(event), response)
        if joined is not None:
            new_event = self._user_fn_output_to_event(event, joined)
            await self._do_downstream(new_event)


def build_flow(steps):
    """Builds a flow from a list of steps, by chaining the steps according to their order in the list.
    Nested lists are used to represent branches in the flow.

    Examples:
        build_flow([step1, step2, step3])
        is equivalent to
        step1.to(step2).to(step3)

        build_flow([step1, [step2a, step2b], step3])
        is equivalent to
        step1.to(step2a)
        step1.to(step3)
        step2a.to(step2b)

    :param steps: a potentially nested list of steps
    :returns: the first step
    :rtype: Flow
    """
    if len(steps) == 0:
        raise ValueError('Cannot build an empty flow')
    cur_step = steps[0]
    for next_step in steps[1:]:
        if isinstance(next_step, list):
            cur_step.to(build_flow(next_step))
        else:
            cur_step.to(next_step)
            cur_step = next_step
    return steps[0]


class Context:
    """
    Context object that holds global secrets and configurations to be passed to relevant steps.

    :param initial_secrets: Initial dict of secrets.
    :param initial_parameters: Initial dict of parameters.
    :param initial_tables: Initial dict of tables.
    """

    def __init__(self, initial_secrets: Optional[Dict[str, str]] = None, initial_parameters: Optional[Dict[str, object]] = None,
                 initial_tables: Optional[Dict[str, Table]] = None):
        self._secrets = initial_secrets or {}
        self._parameters = initial_parameters or {}
        self._tables = initial_tables or {}

    def get_param(self, key, default):
        return self._parameters.get(key, default)

    def set_param(self, key, value):
        self._parameters[key] = value

    def get_secret(self, key):
        return self._secrets.get(key, None)

    def set_secret(self, key, secret):
        self._secrets[key] = secret

    def get_table(self, key):
        return self._tables[key]

    def set_table(self, key, table):
        self._tables[key] = table

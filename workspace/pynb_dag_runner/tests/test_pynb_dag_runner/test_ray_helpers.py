import time
from typing import Any, Awaitable, Callable

#
import pytest, ray

#
from pynb_dag_runner.helpers import A, one, Try
from pynb_dag_runner.ray_helpers import (
    try_f_with_timeout_guard,
    Future,
)
from pynb_dag_runner.opentelemetry_helpers import (
    SpanDict,
    read_key,
    Spans,
    SpanRecorder,
)


@ray.remote(num_cpus=0)
class StateActor:
    def __init__(self):
        self._state = []

    def add(self, value):
        self._state += [value]

    def get(self):
        return self._state


### Test Future static functions


def test_future_value():
    assert ray.get(Future.value(42)) == 42


def test_future_map():
    @ray.remote(num_cpus=0)
    def f() -> int:
        return 123

    # example of a future having Future[int] type, but type checker does not notice
    # any problem with the below code.
    future: Future[bool] = f.remote()

    assert ray.get(Future.map(future, lambda x: x + 1)) == 124


def test_future_lift():
    assert ray.get(Future.lift(lambda x: x + 1)(Future.value(1))) == 2


def test_future_async_lift():
    async def f(x):
        return x + 1

    assert ray.get(Future.lift_async(f)(ray.put(1))) == 2


@pytest.mark.asyncio
async def test_future_async_lift_w_exception():
    async def f(_):
        raise Exception("boom!")

    with pytest.raises(Exception):
        await Future.lift_async(f)("dummy arg to f")

    with pytest.raises(Exception):
        ray.get(Future.lift_async(f)("dummy arg to f"))


### --- tests for try_f_with_timeout_guard wrapper ---


@pytest.mark.asyncio
async def test_timeout_w_success():
    N_calls = 3

    async def get_test_spans():
        with SpanRecorder() as rec:

            def f(x: int) -> int:
                return x + 1

            f_timeout: Callable[[int], Awaitable[Try[int]]] = try_f_with_timeout_guard(
                f, timeout_s=10, num_cpus=1
            )

            for x in range(N_calls):
                assert await f_timeout(x) == Try(x + 1, None)

        return rec.spans

    def validate_spans(spans: Spans):
        func_call_spans: Spans = spans.filter(["name"], "call-python-function")
        assert len(func_call_spans) == N_calls

        for span in func_call_spans:
            assert read_key(span, ["status", "status_code"]) == "OK"

    validate_spans(await get_test_spans())


@pytest.mark.asyncio
async def test_timeout_w_exception():
    N_calls = 3

    async def get_test_spans():
        with SpanRecorder() as rec:

            def error(dummy: int) -> Exception:
                return ValueError(f"BOOM{dummy}")

            def f(dummy: int):
                raise error(dummy)

            f_timeout: Callable[[int], Awaitable[Try[int]]] = try_f_with_timeout_guard(
                f, timeout_s=10, num_cpus=1
            )

            for x in range(N_calls):
                assert await f_timeout(x) == Try(None, error(x))
        return rec.spans

    def validate_spans(spans: Spans):
        func_call_spans: Spans = spans.filter(["name"], "call-python-function")
        assert len(func_call_spans) == N_calls

        for span in func_call_spans:
            assert span["status"] == {"status_code": "ERROR", "description": "Failure"}

            event = one(read_key(span, ["events"]))
            assert set(event.keys()) == set(["name", "timestamp", "attributes"])
            assert event["name"] == "exception"
            assert set(event["attributes"]) == set(
                [
                    "exception.type",
                    "exception.message",
                    "exception.stacktrace",
                    "exception.escaped",
                ]
            )
            assert read_key(event, ["attributes", "exception.type"]) == "ValueError"

    validate_spans(await get_test_spans())


@pytest.mark.asyncio
async def test_timeout_w_timeout_cancel():
    N_calls = 3

    async def get_test_spans():
        with SpanRecorder() as rec:

            def f(_: Any) -> None:
                time.sleep(1e6)

            f_timeout: Callable[[int], Awaitable[Try[int]]] = try_f_with_timeout_guard(
                f, timeout_s=0.5, num_cpus=1
            )

            for _ in range(N_calls):
                result = await f_timeout("argument-to-function-f")
                assert result == Try(
                    value=None,
                    error=Exception(
                        "Timeout error: execution did not finish within timeout limit"
                    ),
                )

        return rec.spans

    def validate_spans(spans: Spans):
        func_call_spans: Spans = spans.filter(["name"], "timeout-guard")
        assert len(func_call_spans) == N_calls

        for span in func_call_spans:
            assert read_key(span, ["attributes", "task.timeout_s"]) == 0.5
            assert span["status"] == {"status_code": "ERROR", "description": "Timeout"}

    validate_spans(await get_test_spans())


# this test has failed randomly (TODO)
@pytest.mark.asyncio
@pytest.mark.parametrize("dummy_loop_parameter", range(1))
@pytest.mark.parametrize("task_timeout_s", [0.001, 10.0])
async def test_timeout_w_timeout(dummy_loop_parameter, task_timeout_s):
    state_actor = StateActor.remote()

    task_duration_s = 0.2

    def f(_: Any) -> int:
        time.sleep(task_duration_s)

        # We should not get here *if* task is canceled by timeout
        state_actor.add.remote("foo")
        return 123

    f_timeout: Callable[[Any], Awaitable[Try[int]]] = try_f_with_timeout_guard(
        f, timeout_s=task_timeout_s, num_cpus=1
    )

    result: Try[int] = await f_timeout("dummy")

    # Wait for task to finish
    time.sleep(4.0)

    state_has_flipped: bool = "foo" in await state_actor.get.remote()

    if task_timeout_s < task_duration_s:
        # f should have been canceled, and state should not have flipped
        assert not state_has_flipped
        assert "timeout" in str(result.error)
    else:
        assert state_has_flipped
        assert result == Try(123, None)


### ---- test Try implementation ----


def test_try_both_value_and_error_can_not_be_set():
    with pytest.raises(Exception):
        assert Try(1, Exception("foo"))


def test_try_is_success_method():
    assert Try(None, None).is_success() == True
    assert Try(12345, None).is_success() == True
    assert Try(None, Exception("Foo")).is_success() == False


def test_try_equality_checking():
    assert Try(None, None) == Try(None, None)

    assert Try(12345, None) == Try(12345, None)
    assert Try(12345, None) != Try(None, None)

    assert Try(None, Exception("foo")) == Try(None, Exception("foo"))
    assert Try(None, Exception("foo")) != Try(None, Exception("bar"))

    assert Try(123, None) != Exception("!!!")
    assert Try(123, None) != (lambda: None)

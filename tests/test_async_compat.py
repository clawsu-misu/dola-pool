"""3.10 没有 asyncio.timeout。兼容层要到点改抛 TimeoutError，并认 reschedule / expired。"""
import asyncio

import pytest

from dola_pool.async_compat import _Timeout, timeout


def _run(coro):
    """用独立的新循环跑，不碰线程的当前循环。

    asyncio.run() 会把当前循环置空，之后依赖 get_event_loop() 的老测试
    就报 no current event loop；这里只建一个局部循环，跑完关掉。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_py310_timeout_becomes_timeout_error():
    async def body():
        async with _Timeout(0.05):
            await asyncio.sleep(2)

    with pytest.raises(TimeoutError):
        _run(body())


def test_py310_timeout_can_be_turned_off():
    async def body():
        async with _Timeout(0.05) as clock:
            clock.reschedule(None)
            await asyncio.sleep(0.1)
            assert clock.expired() is False

    _run(body())


def test_py310_none_means_no_limit_and_expired_sticks():
    async def no_limit():
        async with _Timeout(None) as clock:
            await asyncio.sleep(0.05)
            assert clock.expired() is False

    async def hits():
        clock = _Timeout(0.05)
        with pytest.raises(TimeoutError):
            async with clock:
                await asyncio.sleep(2)
        assert clock.expired() is True

    _run(no_limit())
    _run(hits())


def test_py310_outside_cancel_is_not_a_timeout():
    async def body():
        async with _Timeout(5):
            await asyncio.sleep(5)

    async def scenario():
        task = asyncio.create_task(body())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())


def test_public_timeout_uses_the_stdlib_on_this_python():
    async def body():
        async with timeout(0.05):
            await asyncio.sleep(2)

    if hasattr(asyncio, "timeout"):
        with pytest.raises(TimeoutError):
            _run(body())
    else:
        assert isinstance(timeout(1), _Timeout)

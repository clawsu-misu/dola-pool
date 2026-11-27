"""asyncio.timeout 的兼容层。

项目声明支持 Python 3.10，但 asyncio.timeout 是 3.11 才有的。3.10 上调用会直接
AttributeError，账号验证因此报「服务器异常」。3.11 及以上走标准库，行为不变。
"""
import asyncio


def timeout(delay):
    """delay 为秒；None 表示不限时。返回的对象支持 reschedule(绝对时间或 None) 和 expired()。"""
    native = getattr(asyncio, "timeout", None)
    if native is not None:
        return native(delay)
    return _Timeout(delay)


class _Timeout:
    """3.10 上的最小实现：到点取消当前任务，并改抛 TimeoutError。"""

    def __init__(self, delay):
        self._delay = delay
        self._handle = None
        self._task = None
        self._entered = False
        self._hit = False

    def expired(self):
        return self._hit

    def reschedule(self, when):
        if not self._entered:
            raise RuntimeError("超时还没开始")
        if self._hit:
            raise RuntimeError("超时已经触发")
        self._cancel_handle()
        if when is None:
            self._delay = None
            return
        self._arm(when - asyncio.get_running_loop().time())

    async def __aenter__(self):
        self._task = asyncio.current_task()
        self._entered = True
        if self._delay is not None:
            self._arm(self._delay)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._cancel_handle()
        if not (self._hit and exc_type is asyncio.CancelledError):
            return False
        uncancel = getattr(self._task, "uncancel", None)
        if uncancel is not None:
            while self._task.cancelling():
                uncancel()
        raise TimeoutError from exc

    def _arm(self, delay):
        loop = asyncio.get_running_loop()
        when = 0 if delay <= 0 else delay
        self._handle = loop.call_later(when, self._fire)

    def _fire(self):
        self._handle = None
        self._hit = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def _cancel_handle(self):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

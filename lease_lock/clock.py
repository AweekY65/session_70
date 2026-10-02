"""可注入时钟：所有时间逻辑都通过 Clock 接口获取，测试使用 ManualClock，不依赖真实 sleep。"""
from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """返回当前时间（秒，单调语义由实现保证）。"""
        ...


class SystemClock:
    """生产环境时钟，基于 time.monotonic。"""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """测试时钟：时间只能通过 advance() 显式推进，线程安全。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._cond = threading.Condition()

    def now(self) -> float:
        with self._cond:
            return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        with self._cond:
            self._now += seconds
            self._cond.notify_all()
            return self._now

    def sleep(self, seconds: float) -> None:
        """阻塞直到时钟被推进到目标时间（供自动续约线程使用）。"""
        with self._cond:
            target = self._now + seconds
            while self._now < target:
                self._cond.wait(timeout=0.05)

"""模拟客户端：同步 API + 可选的自动续约后台线程（由注入时钟驱动）。

“客户端暂停”通过 pause() 模拟：续约线程停止续约，如同进程被挂起；
resume() 后客户端恢复操作，但若租约已过期，其旧 token 会被拒绝。
"""
from __future__ import annotations

import threading
from typing import Optional

from .clock import ManualClock
from .errors import LeaseNotHeldError
from .server import Lease, LeaseServer


class LeaseClient:
    def __init__(self, server: LeaseServer, client_id: str, ttl: float = 10.0) -> None:
        self._server = server
        self.client_id = client_id
        self.ttl = ttl
        self._lease: Optional[Lease] = None

    @property
    def lease(self) -> Optional[Lease]:
        return self._lease

    def acquire(self, resource: str) -> Lease:
        self._lease = self._server.acquire(resource, self.client_id, self.ttl)
        return self._lease

    def renew(self, resource: str) -> Lease:
        if self._lease is None:
            raise LeaseNotHeldError("never acquired")
        self._lease = self._server.renew(
            resource, self.client_id, self._lease.token, self.ttl
        )
        return self._lease

    def release(self, resource: str) -> None:
        if self._lease is None:
            raise LeaseNotHeldError("never acquired")
        self._server.release(resource, self.client_id, self._lease.token)
        self._lease = None


class AutoRenewClient(LeaseClient):
    """带后台续约线程的客户端，续约节奏由注入时钟驱动，测试无需真实 sleep。"""

    def __init__(
        self,
        server: LeaseServer,
        client_id: str,
        clock: ManualClock,
        ttl: float = 10.0,
        renew_interval: float = 3.0,
    ) -> None:
        super().__init__(server, client_id, ttl)
        self._clock = clock
        self._renew_interval = renew_interval
        self._resource: Optional[str] = None
        self._paused = threading.Event()
        self._paused.set()  # set 表示“未暂停”
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.renew_errors: list[Exception] = []

    def acquire(self, resource: str) -> Lease:
        lease = super().acquire(resource)
        self._resource = resource
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._renew_loop, name=f"renew-{self.client_id}", daemon=True
        )
        self._thread.start()
        return lease

    def pause(self) -> None:
        """模拟客户端进程被挂起（如 GC 停顿 / SIGSTOP）。"""
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def close(self) -> None:
        self._stop.set()
        self._paused.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _renew_loop(self) -> None:
        while not self._stop.is_set():
            self._paused.wait(timeout=0.05)
            if self._stop.is_set() or not self._paused.is_set():
                continue
            self._clock.sleep(self._renew_interval)
            if self._stop.is_set() or not self._paused.is_set():
                continue
            try:
                self.renew(self._resource)  # type: ignore[arg-type]
            except Exception as exc:  # 租约丢失（过期被抢占）时记录并退出
                self.renew_errors.append(exc)
                return

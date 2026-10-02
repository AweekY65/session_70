"""Lease 锁服务端：单进程内模拟，线程安全，状态可持久化到本地文件。

租约状态机（针对单个资源）：
    FREE --acquire--> HELD --release--> FREE
    HELD --renew----> HELD (expires_at 延长)
    HELD --时钟推进超过 expires_at--> EXPIRED --acquire--> HELD(新持有者, 新 token)
EXPIRED 不是显式存储的状态，而是由 clock.now() >= expires_at 推导得出。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .clock import Clock
from .errors import LeaseHeldError, LeaseNotHeldError
from .store import MemoryStore, OpLog, StateStore


@dataclass(frozen=True)
class Lease:
    resource: str
    holder_id: str
    token: int
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class LeaseServer:
    """管理多个资源的租约。所有方法都可被多线程（模拟多客户端）并发调用。"""

    def __init__(
        self,
        clock: Clock,
        store: Optional[StateStore] = None,
        op_log: Optional[OpLog] = None,
    ) -> None:
        self._clock = clock
        self._store = store if store is not None else MemoryStore()
        self._log = op_log if op_log is not None else OpLog(None)
        self._mu = threading.Lock()
        persisted = self._store.load()
        # fencing token 计数器持久化，重启后继续单调递增
        self._fencing_token: int = int(persisted.get("fencing_token", 0))
        self._leases: Dict[str, Lease] = {
            name: Lease(resource=name, **rec)
            for name, rec in persisted.get("leases", {}).items()
        }

    # ------------------------------------------------------------------ API
    def acquire(self, resource: str, holder_id: str, ttl: float) -> Lease:
        """获取租约。资源被未过期租约占用时抛 LeaseHeldError。"""
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._mu:
            now = self._clock.now()
            current = self._leases.get(resource)
            if current is not None and not current.is_expired(now):
                self._log.append(
                    "acquire_denied", resource=resource, holder=holder_id,
                    held_by=current.holder_id, now=now,
                )
                raise LeaseHeldError(
                    f"{resource!r} held by {current.holder_id!r} "
                    f"until {current.expires_at}"
                )
            lease = self._grant(resource, holder_id, ttl, now)
            self._log.append(
                "acquire", resource=resource, holder=holder_id,
                token=lease.token, expires_at=lease.expires_at, now=now,
            )
            return lease

    def renew(self, resource: str, holder_id: str, token: int, ttl: float) -> Lease:
        """续期。只有持有未过期租约且 token 匹配的持有者才能成功。"""
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._mu:
            now = self._clock.now()
            current = self._leases.get(resource)
            if (
                current is None
                or current.holder_id != holder_id
                or current.token != token
                or current.is_expired(now)
            ):
                self._log.append(
                    "renew_denied", resource=resource, holder=holder_id,
                    token=token, now=now,
                )
                raise LeaseNotHeldError(
                    f"{holder_id!r} does not hold a valid lease on {resource!r}"
                )
            renewed = Lease(resource, holder_id, token, now + ttl)
            self._leases[resource] = renewed
            self._persist_locked()
            self._log.append(
                "renew", resource=resource, holder=holder_id,
                token=token, expires_at=renewed.expires_at, now=now,
            )
            return renewed

    def release(self, resource: str, holder_id: str, token: int) -> None:
        """释放租约。token 不匹配（如旧持有者）不能释放他人的租约。"""
        with self._mu:
            now = self._clock.now()
            current = self._leases.get(resource)
            if (
                current is None
                or current.holder_id != holder_id
                or current.token != token
            ):
                self._log.append(
                    "release_denied", resource=resource, holder=holder_id,
                    token=token, now=now,
                )
                raise LeaseNotHeldError(
                    f"{holder_id!r} does not hold lease on {resource!r}"
                )
            del self._leases[resource]
            self._persist_locked()
            self._log.append(
                "release", resource=resource, holder=holder_id,
                token=token, now=now,
            )

    def inspect(self, resource: str) -> Optional[Lease]:
        """返回当前有效租约；过期或不存在返回 None。"""
        with self._mu:
            current = self._leases.get(resource)
            if current is None or current.is_expired(self._clock.now()):
                return None
            return current

    # ------------------------------------------------------------- internal
    def _grant(self, resource: str, holder_id: str, ttl: float, now: float) -> Lease:
        self._fencing_token += 1
        lease = Lease(resource, holder_id, self._fencing_token, now + ttl)
        self._leases[resource] = lease
        self._persist_locked()
        return lease

    def _persist_locked(self) -> None:
        self._store.save(
            {
                "fencing_token": self._fencing_token,
                "leases": {
                    name: {
                        "holder_id": lease.holder_id,
                        "token": lease.token,
                        "expires_at": lease.expires_at,
                    }
                    for name, lease in self._leases.items()
                },
            }
        )

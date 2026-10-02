"""受 fencing token 保护的下游资源（本地文件/内存模拟）。

即使锁服务放行了过期持有者（例如客户端暂停后恢复仍以为自己持有锁），
下游资源也会拒绝 token 小于等于已见最大 token 的写入，从而防止脑裂写。
"""
from __future__ import annotations

import threading
from typing import Optional

from .errors import StaleFencingTokenError


class FencedResource:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._last_token = 0
        self._data: Optional[str] = None

    def write(self, token: int, data: str) -> None:
        with self._mu:
            if token <= self._last_token:
                raise StaleFencingTokenError(
                    f"stale fencing token {token}, last seen {self._last_token}"
                )
            self._last_token = token
            self._data = data

    def read(self) -> Optional[str]:
        with self._mu:
            return self._data

    @property
    def last_token(self) -> int:
        with self._mu:
            return self._last_token

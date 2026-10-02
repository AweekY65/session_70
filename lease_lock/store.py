"""本地持久化：锁状态、fencing token 与操作日志只写入本地文件或内存。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Protocol


class StateStore(Protocol):
    def load(self) -> Dict[str, Any]: ...
    def save(self, state: Dict[str, Any]) -> None: ...


class MemoryStore:
    """纯内存存储，不落盘。"""

    def __init__(self) -> None:
        self._state: Dict[str, Any] = {}
        self._mu = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._mu:
            return json.loads(json.dumps(self._state))

    def save(self, state: Dict[str, Any]) -> None:
        with self._mu:
            self._state = json.loads(json.dumps(state))


class FileStore:
    """JSON 文件存储，原子写入（tmp + rename），保证崩溃时不出现半截文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mu = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._mu:
            if not self.path.exists():
                return {}
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)

    def save(self, state: Dict[str, Any]) -> None:
        with self._mu:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


class OpLog:
    """本地 JSONL 操作日志，便于审计与调试。"""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._mu = threading.Lock()

    def append(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"event": event, **fields}
        with self._mu:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")

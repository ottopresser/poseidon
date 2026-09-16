from __future__ import annotations

from collections import deque
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid


logger = logging.getLogger("poseidon.audit")


class OperationAuditStore:
    def __init__(self, max_records: int | None = None, log_path: str | None = None) -> None:
        configured_max = (
            max_records
            if max_records is not None
            else int(os.environ.get("POSEIDON_AUDIT_MAX_RECORDS", "1000"))
        )
        if configured_max <= 0:
            raise ValueError("POSEIDON_AUDIT_MAX_RECORDS must be greater than zero.")
        self._records: deque[dict[str, object]] = deque(maxlen=configured_max)
        self._log_path = log_path if log_path is not None else os.environ.get("POSEIDON_AUDIT_LOG")
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        if not self._log_path:
            return
        path = Path(self._log_path)
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                required = {
                    "operation_id",
                    "timestamp_epoch",
                    "method",
                    "path",
                    "status_code",
                    "duration_ms",
                    "client",
                }
                if isinstance(record, dict) and required <= record.keys():
                    self._records.append(record)
        except OSError as exc:
            logger.warning("Unable to read audit log %s: %s", path, exc)

    def record(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        client: str,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "operation_id": str(uuid.uuid4()),
            "timestamp_epoch": int(time.time()),
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "client": client,
        }
        with self._lock:
            self._records.append(record)
            if self._log_path:
                try:
                    log_path = Path(self._log_path)
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as log_file:
                        log_file.write(json.dumps(record, separators=(",", ":")) + "\n")
                except OSError as exc:
                    logger.warning("Unable to append audit log %s: %s", self._log_path, exc)
        return record

    def list_records(self, limit: int) -> list[dict[str, object]]:
        if limit < 1:
            return []
        with self._lock:
            return list(self._records)[-limit:][::-1]
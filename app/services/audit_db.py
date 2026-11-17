"""Separate audit database with a non-blocking writer.

record() only enqueues (never raises, never blocks the request path); a background task drains the queue.
Raw SQL, tokens and result sets are never stored: SQL is kept as a sha256 hash.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Column, Float, Integer, MetaData, String, Table, Text, insert, select

from app.core.config import settings
from app.core.logger import logger
from app.services.session_store import make_engine

_DROP_KEYS = {"authorization", "token", "password", "secret", "jwt", "results", "rows", "api_key"}


def _clean(detail: Any) -> Any:
    if isinstance(detail, dict):
        return {k: _clean(v) for k, v in detail.items() if str(k).lower() not in _DROP_KEYS}
    if isinstance(detail, (list, tuple)):
        return [_clean(v) for v in detail][:50]
    if isinstance(detail, str):
        return detail[:500]
    return detail


class AuditDB:
    def __init__(self, url: Optional[str] = None, queue_size: Optional[int] = None):
        self.engine = make_engine(url or settings.AUDIT_DATABASE_URL)
        md = MetaData()
        self.table = Table("audit_events", md, Column("id", Integer, primary_key=True, autoincrement=True),
                           Column("created_at", String(40), nullable=False, index=True),
                           Column("user_id", String(64), index=True), Column("session_id", String(64)),
                           Column("action", String(40), index=True), Column("outcome", String(24)),
                           Column("role", String(16)), Column("source", String(24)), Column("sql_hash", String(64)),
                           Column("row_count", Integer), Column("cache_hit", Boolean), Column("latency_ms", Float),
                           Column("detail", Text))
        md.create_all(self.engine)
        self._queue: Optional["asyncio.Queue"] = None
        self._size = queue_size or settings.AUDIT_QUEUE_SIZE
        self._task: Optional["asyncio.Task"] = None
        self._loop = None
        self.dropped = 0

    def _q(self) -> "asyncio.Queue":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._queue is not None and loop is not None and self._loop is not None and loop is not self._loop:
            # the event loop changed (tests, reloads): carry pending events over to a queue bound to the new loop
            old, self._queue, self._task = self._queue, None, None
            pending = []
            while not old.empty():
                pending.append(old.get_nowait())
            self._queue = asyncio.Queue(maxsize=self._size)
            for row in pending:
                self._queue.put_nowait(row)
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._size)
        if loop is not None:
            self._loop = loop
        return self._queue

    def _to_row(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        sql = ev.get("sql")
        return {"created_at": ev.get("created_at") or datetime.now(timezone.utc).isoformat(),
                "user_id": ev.get("user_id"), "session_id": ev.get("session_id"), "action": ev.get("action"),
                "outcome": ev.get("outcome"), "role": ev.get("role"), "source": ev.get("source"),
                "sql_hash": hashlib.sha256(sql.encode()).hexdigest() if isinstance(sql, str) and sql else ev.get("sql_hash"),
                "row_count": ev.get("row_count"), "cache_hit": ev.get("cache_hit"),
                "latency_ms": ev.get("latency_ms"),
                "detail": json.dumps(_clean(ev.get("detail") or {}), default=str)}

    def record(self, event: Dict[str, Any]) -> None:
        try:
            self._q().put_nowait(self._to_row(event))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning("Audit queue full; event dropped")
        except Exception as e:
            self.dropped += 1
            logger.warning(f"Audit record failed: {e}")

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        with self.engine.begin() as c:
            c.execute(insert(self.table), rows)

    async def _drain(self) -> None:
        q = self._q()
        while True:
            rows = [await q.get()]
            while not q.empty() and len(rows) < 100:
                rows.append(q.get_nowait())
            try:
                await asyncio.to_thread(self._write, rows)
            except Exception as e:
                logger.error(f"Audit write failed ({len(rows)} events lost): {e}")
            for _ in rows:
                q.task_done()

    async def start(self) -> None:
        self._q()  # rebinds to the running loop if it changed
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def flush(self) -> None:
        if self._task is None or self._task.done():
            await self.start()
        await self._q().join()

    async def stop(self) -> None:
        await self.flush()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def query(self, user_id: Optional[str] = None, action: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        q = select(self.table)
        if user_id is not None:
            q = q.where(self.table.c.user_id == user_id)
        if action is not None:
            q = q.where(self.table.c.action == action)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(self.table.c.id.desc()).limit(max(limit, 0))).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.get("detail") or "{}")
            out.append(d)
        return out

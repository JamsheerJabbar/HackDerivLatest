"""Human review threads for insights users rated false. Owner and staff (admin/super) can read and post."""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, update

from app.core.config import settings
from app.services.session_store import make_engine

STAFF_ROLES = ("admin", "super")
AUTHOR_ROLES = ("user", "human", "system")


class HandoffNotFound(Exception):
    pass


class HandoffForbidden(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HandoffStore:
    def __init__(self, url: Optional[str] = None):
        self.engine = make_engine(url or settings.SESSION_DATABASE_URL)
        md = MetaData()
        self.threads = Table("handoff_threads", md, Column("thread_id", String(36), primary_key=True),
                             Column("insight_id", String(36), nullable=False, index=True),
                             Column("user_id", String(64), nullable=False, index=True),
                             Column("status", String(16), nullable=False), Column("reason", Text),
                             Column("created_at", String(40), nullable=False), Column("resolved_at", String(40)))
        self.messages = Table("handoff_messages", md, Column("id", Integer, primary_key=True, autoincrement=True),
                              Column("thread_id", String(36), nullable=False, index=True),
                              Column("author_id", String(64)), Column("author_role", String(16), nullable=False),
                              Column("content", Text, nullable=False), Column("created_at", String(40), nullable=False))
        md.create_all(self.engine)

    def open_thread(self, insight_id: str, user_id: str, reason: Optional[str] = None) -> str:
        with self.engine.begin() as c:
            existing = c.execute(select(self.threads.c.thread_id).where(
                self.threads.c.insight_id == insight_id, self.threads.c.status == "open")).first()
            if existing:
                return existing.thread_id
            tid = str(uuid.uuid4())
            c.execute(insert(self.threads).values(thread_id=tid, insight_id=insight_id, user_id=user_id,
                                                  status="open", reason=reason, created_at=_now()))
            c.execute(insert(self.messages).values(
                thread_id=tid, author_id=None, author_role="system", created_at=_now(),
                content="This insight was rated incorrect. A reviewer has been notified and will reply here; "
                        "you can add details in the meantime."))
        return tid

    def post_message(self, thread_id: str, author_id: str, author_role: str, content: str) -> int:
        if author_role not in AUTHOR_ROLES:
            raise ValueError(f"invalid author_role: {author_role}")
        if not (content or "").strip():
            raise ValueError("content must not be empty")
        with self.engine.begin() as c:
            if c.execute(select(self.threads.c.thread_id).where(self.threads.c.thread_id == thread_id)).first() is None:
                raise HandoffNotFound(thread_id)
            res = c.execute(insert(self.messages).values(thread_id=thread_id, author_id=author_id,
                                                         author_role=author_role, content=content, created_at=_now()))
            return int(res.inserted_primary_key[0])

    def _thread_row(self, thread_id: str):
        with self.engine.connect() as c:
            row = c.execute(select(self.threads).where(self.threads.c.thread_id == thread_id)).first()
        if row is None:
            raise HandoffNotFound(thread_id)
        return row

    def _authorize(self, row, user_id: str, role: str) -> None:
        if row.user_id != user_id and role not in STAFF_ROLES:
            raise HandoffForbidden(row.thread_id)

    def get_thread(self, thread_id: str, user_id: str, role: str) -> Dict[str, Any]:
        row = self._thread_row(thread_id)
        self._authorize(row, user_id, role)
        with self.engine.connect() as c:
            msgs = c.execute(select(self.messages).where(self.messages.c.thread_id == thread_id)
                             .order_by(self.messages.c.id)).all()
        return {"thread_id": row.thread_id, "insight_id": row.insight_id, "user_id": row.user_id,
                "status": row.status, "reason": row.reason, "created_at": row.created_at,
                "messages": [{"author_id": m.author_id, "author_role": m.author_role, "content": m.content,
                              "created_at": m.created_at} for m in msgs]}

    def list_threads(self, user_id: str, role: str) -> List[Dict[str, Any]]:
        q = select(self.threads)
        q = q.where(self.threads.c.status == "open") if role in STAFF_ROLES else q.where(self.threads.c.user_id == user_id)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(self.threads.c.created_at.desc())).all()
        return [{"thread_id": r.thread_id, "insight_id": r.insight_id, "user_id": r.user_id, "status": r.status,
                 "reason": r.reason, "created_at": r.created_at} for r in rows]

    def resolve_thread(self, thread_id: str, user_id: str, role: str) -> None:
        row = self._thread_row(thread_id)
        if role not in STAFF_ROLES:
            raise HandoffForbidden(thread_id)
        with self.engine.begin() as c:
            c.execute(update(self.threads).where(self.threads.c.thread_id == thread_id)
                      .values(status="resolved", resolved_at=_now()))

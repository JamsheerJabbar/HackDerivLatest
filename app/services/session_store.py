"""Per-user chat sessions and messages. Every read/write is scoped by user_id; foreign sessions are forbidden."""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, insert, select
from sqlalchemy.pool import StaticPool

from app.core.config import settings


class SessionForbidden(Exception):
    pass


class SessionNotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_engine(url: str):
    kw: Dict[str, Any] = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool
    return create_engine(url, **kw)


class SessionStore:
    def __init__(self, url: Optional[str] = None):
        self.engine = make_engine(url or settings.SESSION_DATABASE_URL)
        md = MetaData()
        self.sessions = Table("chat_sessions", md, Column("session_id", String(36), primary_key=True),
                              Column("user_id", String(64), nullable=False, index=True),
                              Column("created_at", String(40), nullable=False), Column("seq", Integer))
        self.messages = Table("chat_messages", md, Column("id", Integer, primary_key=True, autoincrement=True),
                              Column("session_id", String(36), nullable=False, index=True),
                              Column("user_id", String(64), nullable=False),
                              Column("role", String(16), nullable=False), Column("content", Text, nullable=False),
                              Column("metadata", Text), Column("created_at", String(40), nullable=False))
        md.create_all(self.engine)

    def create_session(self, user_id: str) -> str:
        sid = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(insert(self.sessions).values(session_id=sid, user_id=user_id, created_at=_now()))
        return sid

    def assert_owner(self, session_id: str, user_id: str) -> None:
        with self.engine.connect() as c:
            row = c.execute(select(self.sessions.c.user_id).where(self.sessions.c.session_id == session_id)).first()
        if row is None:
            raise SessionNotFound(session_id)
        if row.user_id != user_id:
            raise SessionForbidden(session_id)

    def append_message(self, session_id: str, user_id: str, role: str, content: str,
                       metadata: Optional[Dict[str, Any]] = None) -> int:
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"invalid role: {role}")
        self.assert_owner(session_id, user_id)
        with self.engine.begin() as c:
            res = c.execute(insert(self.messages).values(
                session_id=session_id, user_id=user_id, role=role, content=content,
                metadata=json.dumps(metadata or {}, default=str), created_at=_now()))
            return int(res.inserted_primary_key[0])

    def history(self, session_id: str, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        self.assert_owner(session_id, user_id)
        with self.engine.connect() as c:
            rows = c.execute(select(self.messages).where(self.messages.c.session_id == session_id,
                                                         self.messages.c.user_id == user_id)
                             .order_by(self.messages.c.id.desc()).limit(max(limit, 0))).all()
        return [{"role": r.role, "content": r.content, "metadata": json.loads(r.metadata or "{}"),
                 "created_at": r.created_at} for r in reversed(rows)]

    def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(self.sessions).where(self.sessions.c.user_id == user_id)
                             .order_by(self.sessions.c.created_at.desc())).all()
        return [{"session_id": r.session_id, "created_at": r.created_at} for r in rows]

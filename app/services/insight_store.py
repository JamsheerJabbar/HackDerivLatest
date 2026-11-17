"""Timestamped insight storage, isolated per user."""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from datetime import timedelta

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, and_, insert, select, update

from app.core.config import settings
from app.services.session_store import make_engine

SOURCES = ("chat", "sentinel")


class InsightNotFound(Exception):
    """Unknown insight, or one the caller does not own (indistinguishable on purpose)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InsightStore:
    def __init__(self, url: Optional[str] = None):
        self.engine = make_engine(url or settings.SESSION_DATABASE_URL)
        md = MetaData()
        self.table = Table("insights", md, Column("insight_id", String(36), primary_key=True),
                           Column("user_id", String(64), nullable=False, index=True),
                           Column("session_id", String(64)), Column("source", String(16), nullable=False),
                           Column("question", Text), Column("insight", Text, nullable=False), Column("sql", Text),
                           Column("metadata", Text), Column("created_at", String(40), nullable=False, index=True))
        self.scans = Table("scan_owners", md, Column("scan_id", String(64), primary_key=True),
                           Column("user_id", String(64), nullable=False, index=True),
                           Column("created_at", String(40), nullable=False))
        self.ratings = Table("insight_ratings", md, Column("insight_id", String(36), primary_key=True),
                             Column("user_id", String(64), primary_key=True), Column("rating", Integer, nullable=False),
                             Column("created_at", String(40), nullable=False),
                             Column("updated_at", String(40), nullable=False))
        self.flags = Table("insight_anomaly_flags", md, Column("flag_id", String(36), primary_key=True),
                           Column("user_id", String(64), nullable=False, index=True),
                           Column("source", String(16), nullable=False), Column("false_count", Integer),
                           Column("status", String(16), nullable=False), Column("created_at", String(40), nullable=False),
                           Column("acknowledged_at", String(40)))
        md.create_all(self.engine)

    # ── ratings (owner only; one row per insight+user) ────────────────────────
    def rate_insight(self, insight_id: str, user_id: str, rating: bool) -> Dict[str, Any]:
        if self.get_insight(insight_id, user_id) is None:
            raise InsightNotFound(insight_id)
        val = 1 if rating else 0
        with self.engine.begin() as c:
            cur = c.execute(select(self.ratings.c.rating).where(self.ratings.c.insight_id == insight_id,
                                                                self.ratings.c.user_id == user_id)).first()
            if cur is None:
                c.execute(insert(self.ratings).values(insight_id=insight_id, user_id=user_id, rating=val,
                                                      created_at=_now(), updated_at=_now()))
                changed = True
            else:
                changed = cur.rating != val
                if changed:
                    c.execute(update(self.ratings).where(self.ratings.c.insight_id == insight_id,
                                                         self.ratings.c.user_id == user_id)
                              .values(rating=val, updated_at=_now()))
        return {"insight_id": insight_id, "rating": bool(rating), "changed": changed}

    def get_rating(self, insight_id: str, user_id: str) -> Optional[bool]:
        with self.engine.connect() as c:
            r = c.execute(select(self.ratings.c.rating).where(self.ratings.c.insight_id == insight_id,
                                                              self.ratings.c.user_id == user_id)).first()
        return None if r is None else bool(r.rating)

    def _false_ids(self, user_id: str, source: str, window_days: Optional[int]) -> List[str]:
        cond = [self.table.c.user_id == user_id, self.table.c.source == source,
                self.ratings.c.user_id == user_id, self.ratings.c.rating == 0]
        if window_days is not None:
            cond.append(self.ratings.c.updated_at >= (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat())
        q = select(self.table.c.insight_id).select_from(
            self.table.join(self.ratings, self.table.c.insight_id == self.ratings.c.insight_id)).where(and_(*cond))
        with self.engine.connect() as c:
            return [r.insight_id for r in c.execute(q)]

    def false_rating_count(self, user_id: str, source: str = "sentinel", window_days: Optional[int] = None) -> int:
        return len(self._false_ids(user_id, source, window_days))

    def anomaly_status(self, user_id: str, source: str = "sentinel", threshold: Optional[int] = None,
                       window_days: Optional[int] = None) -> Dict[str, Any]:
        threshold = settings.ANOMALY_THRESHOLD if threshold is None else threshold
        window_days = settings.ANOMALY_WINDOW_DAYS if window_days is None else window_days
        ids = self._false_ids(user_id, source, window_days)
        return {"flagged": len(ids) > threshold, "false_count": len(ids), "threshold": threshold,
                "window_days": window_days, "insight_ids": ids}

    # ── anomaly flags ────────────────────────────────────────────────────────
    def record_anomaly_flag(self, user_id: str, source: str, false_count: int) -> str:
        with self.engine.begin() as c:
            open_ = c.execute(select(self.flags.c.flag_id).where(
                self.flags.c.user_id == user_id, self.flags.c.source == source,
                self.flags.c.status == "flagged")).first()
            if open_:
                return open_.flag_id
            fid = str(uuid.uuid4())
            c.execute(insert(self.flags).values(flag_id=fid, user_id=user_id, source=source,
                                                false_count=false_count, status="flagged", created_at=_now()))
            return fid

    def list_anomaly_flags(self, user_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        q = select(self.flags).where(self.flags.c.user_id == user_id)
        if status:
            q = q.where(self.flags.c.status == status)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(self.flags.c.created_at.desc())).all()
        return [{"flag_id": r.flag_id, "user_id": r.user_id, "source": r.source, "false_count": r.false_count,
                 "status": r.status, "created_at": r.created_at, "acknowledged_at": r.acknowledged_at} for r in rows]

    def acknowledge_anomaly(self, flag_id: str, user_id: str) -> None:
        with self.engine.begin() as c:
            res = c.execute(update(self.flags).where(self.flags.c.flag_id == flag_id, self.flags.c.user_id == user_id)
                            .values(status="acknowledged", acknowledged_at=_now()))
            if res.rowcount == 0:
                raise InsightNotFound(flag_id)

    def register_scan(self, scan_id: str, user_id: str) -> None:
        """Record who owns a Sentinel scan (first registration wins) so history reads can be user-scoped."""
        with self.engine.begin() as c:
            if c.execute(select(self.scans.c.scan_id).where(self.scans.c.scan_id == scan_id)).first() is None:
                c.execute(insert(self.scans).values(scan_id=scan_id, user_id=user_id,
                                                    created_at=datetime.now(timezone.utc).isoformat()))

    def owned_scan_ids(self, user_id: str) -> List[str]:
        with self.engine.connect() as c:
            return [r.scan_id for r in c.execute(select(self.scans.c.scan_id).where(self.scans.c.user_id == user_id))]

    def owns_scan(self, scan_id: str, user_id: str) -> bool:
        with self.engine.connect() as c:
            return c.execute(select(self.scans.c.scan_id).where(self.scans.c.scan_id == scan_id,
                                                                self.scans.c.user_id == user_id)).first() is not None

    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return {"insight_id": r.insight_id, "user_id": r.user_id, "session_id": r.session_id, "source": r.source,
                "question": r.question, "insight": json.loads(r.insight), "sql": r.sql,
                "metadata": json.loads(r.metadata or "{}"), "created_at": r.created_at}

    def save_insight(self, user_id: str, session_id: Optional[str], source: str, question: str, insight: Any,
                     sql: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        if source not in SOURCES:
            raise ValueError(f"invalid source: {source}")
        iid = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(insert(self.table).values(
                insight_id=iid, user_id=user_id, session_id=session_id, source=source, question=question,
                insight=json.dumps(insight, default=str), sql=sql, metadata=json.dumps(metadata or {}, default=str),
                created_at=datetime.now(timezone.utc).isoformat()))
        return iid

    def get_insight(self, insight_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as c:
            r = c.execute(select(self.table).where(self.table.c.insight_id == insight_id,
                                                   self.table.c.user_id == user_id)).first()
        return self._row(r) if r else None

    def list_insights(self, user_id: str, source: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        q = select(self.table).where(self.table.c.user_id == user_id)
        if source:
            q = q.where(self.table.c.source == source)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(self.table.c.created_at.desc(), self.table.c.insight_id).limit(max(limit, 0))).all()
        return [self._row(r) for r in rows]

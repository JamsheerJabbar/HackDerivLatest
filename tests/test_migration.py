"""Offline tests for the migration helpers (PostgreSQL-backed behaviour lives in tests/integration)."""
from datetime import date, datetime

import pytest
from sqlalchemy import Column, create_engine
from sqlalchemy.types import JSON, Boolean, Date, DateTime, Integer

from app.files import migrate_to_postgres as mig
from tests.legacy_db import make_legacy_db


def test_translate_mysql_and_sqlite_flavours():
    out = mig.translate_to_postgres("""
        -- comment; with semicolon
        CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, f TINYINT(1), u TIMESTAMP ON UPDATE CURRENT_TIMESTAMP);
        CREATE INDEX idx_a ON t(f);
        CREATE VIEW IF NOT EXISTS v AS SELECT 1;
        INSERT IGNORE INTO t (id) VALUES (1);
        INSERT INTO t (id) VALUES (2);
    """)
    joined = "\n".join(out)
    assert "SERIAL PRIMARY KEY" in joined and "SMALLINT" in joined and "ON UPDATE" not in joined
    assert "CREATE INDEX IF NOT EXISTS idx_a" in joined and "CREATE OR REPLACE VIEW v" in joined
    assert "INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING" in joined
    assert "INSERT INTO t (id) VALUES (2) ON CONFLICT DO NOTHING" in joined
    assert len(out) == 5


def test_analyze_and_missing_required_columns_for_legacy(tmp_path):
    physical, profile = mig.analyze_source(create_engine(make_legacy_db(tmp_path / "l.db")))
    assert profile == "legacy_live" and set(physical.tables) == {"users", "transactions", "login_events"}
    missing = mig.missing_required_columns(physical, profile)
    # canonical NOT NULL columns the legacy file cannot provide: reported, never invented
    assert {"users.email", "users.country"} <= set(missing)
    assert "users.full_name" not in missing          # mapped from username


def test_convert_values():
    st = mig.Stats()
    c = lambda t: Column("c", t)
    assert mig.convert("t", c(Boolean()), 1, st) is True
    assert mig.convert("t", c(DateTime()), "3/31/24 6:07", st) == datetime(2024, 3, 31, 6, 7)
    assert mig.convert("t", c(DateTime()), "2023-01-15T09:30:00Z", st) == datetime(2023, 1, 15, 9, 30)
    assert mig.convert("t", c(Date()), "2024-02-29", st) == date(2024, 2, 29)
    assert mig.convert("t", c(DateTime()), "", st) is None and not st.unparsable
    assert mig.convert("t", c(DateTime()), "garbage", st) is None and st.unparsable == {"t.c": 1}
    assert mig.convert("t", c(JSON()), '{"a": 1}', st) == {"a": 1}
    assert mig.convert("t", c(JSON()), "legacy plain text", st) == "legacy plain text"   # no abort (ISS-17)
    assert mig.convert("t", c(Integer()), None, st) is None and mig.convert("t", c(Integer()), 7, st) == 7

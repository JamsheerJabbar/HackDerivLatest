"""Builds a SQLite file with the LIVE (legacy) shape of derivinsightnew.db, including its messy values:
users.username (no email/full_name/country), INTEGER is_pep, and timestamps in three text formats."""
import sqlite3
from pathlib import Path

LEGACY_DDL = """
CREATE TABLE users (user_id VARCHAR(36) PRIMARY KEY, username VARCHAR(200), age INTEGER, kyc_status VARCHAR(20),
  risk_level VARCHAR(10), risk_score INTEGER, is_pep INTEGER, account_status VARCHAR(20),
  created_at TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE transactions (txn_id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), txn_type VARCHAR(20),
  instrument VARCHAR(20), amount DECIMAL(18,2), currency CHAR(3), amount_usd DECIMAL(18,2), status VARCHAR(20),
  flag_reason VARCHAR(100), payment_method VARCHAR(50), external_ref VARCHAR(100), ip_address VARCHAR(45),
  created_at TIMESTAMP, processed_at TIMESTAMP);
CREATE TABLE login_events (event_id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), email_attempted VARCHAR(255),
  ip_address VARCHAR(45), country VARCHAR(100), city VARCHAR(100), device_type VARCHAR(20),
  device_fingerprint VARCHAR(64), user_agent TEXT, status VARCHAR(20), failure_reason VARCHAR(50),
  created_at TIMESTAMP);
"""

USERS = [("1", "john_smith", 34, "VERIFIED", "LOW", 12, 0, "ACTIVE", "2023-01-15T09:30:00Z", "2025-12-10T14:22:00Z"),
         ("2", "emma_wilson", 28, "VERIFIED", "HIGH", 70, 1, "ACTIVE", "2023-02-20T11:45:00Z", "2025-11-28T09:15:00Z"),
         ("3", "li_wei", 41, "PENDING", "MEDIUM", 40, 1, "SUSPENDED", "2022-11-05T14:20:00Z", "2025-12-05T16:30:00Z")]
# US 'M/D/YY H:MM' text, as in the live transactions table
TXNS = [("T1", "1", "DEPOSIT", "EUR/USD", 100.0, "USD", 100.0, "COMPLETED", None, "Bank Transfer", "E1", "1.1.1.1", "3/31/24 6:07", "3/31/24 18:08"),
        ("T2", "1", "TRADE", "GOLD", 250.5, "AUD", 160.25, "FLAGGED", "velocity", "Credit Card", "E2", "1.1.1.2", "5/29/25 18:11", "5/30/25 3:00"),
        ("T3", "2", "WITHDRAWAL", "BTC/USD", 900.0, "USD", 900.0, "FLAGGED", "", "Crypto Wallet", "E3", "1.1.1.3", "12/1/25 9:05", None),
        ("T4", "3", "REFUND", "AMZN", 40.0, "USD", 40.0, "FAILED", None, "SEPA", "E4", "1.1.1.4", "1/2/2024 10:00", "1/2/2024 11:00")]
LOGINS = [("L1", "1", "a@x.com", "9.9.9.9", "Saudi Arabia", "Dammam", "Desktop", "fp1", "UA", "FAILED", "Invalid email", "2024-04-18 21:53:35"),
          ("L2", "2", "b@x.com", "9.9.9.9", "Singapore", "Singapore", "Mobile", "fp2", "UA", "SUCCESS", None, "2025-10-14 21:13:05"),
          ("L3", "2", "b@x.com", "8.8.8.8", "India", "Mumbai", "Desktop", "fp3", "UA", "FAILED", "Bad pw", "2025-11-01 08:00:00")]


def make_legacy_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?)", USERS)
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", TXNS)
    conn.executemany("INSERT INTO login_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", LOGINS)
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"

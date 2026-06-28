"""SQLite-backed storage: a `submissions` table tracking each piece of content
and its status, and an `audit_log` table that records every attribution decision
and every appeal as a structured, queryable event.

Every decision logs the confidence score, the signals used, and (for appeals)
the creator's reasoning alongside the original decision — see planning.md §4.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("PROVENANCE_DB", "provenance_guard.db")


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id   TEXT PRIMARY KEY,
            creator_id   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            text_excerpt TEXT NOT NULL,
            verdict      TEXT NOT NULL,
            confidence   REAL NOT NULL,
            combined_p_ai REAL NOT NULL,
            label_variant TEXT NOT NULL,
            status       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            entry_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            event_type TEXT NOT NULL,            -- 'submission' | 'appeal'
            timestamp  TEXT NOT NULL,
            details    TEXT NOT NULL             -- JSON blob
        );
        """
    )
    conn.commit()
    conn.close()


def _excerpt(text, n=160):
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def record_submission(content_id, creator_id, text, decision, label):
    """Persist a submission + its decision, and write a 'submission' audit entry."""
    conn = get_conn()
    ts = _now()
    conn.execute(
        """INSERT INTO submissions
           (content_id, creator_id, created_at, text_excerpt, verdict, confidence,
            combined_p_ai, label_variant, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            content_id,
            creator_id,
            ts,
            _excerpt(text),
            decision["verdict"],
            decision["confidence"],
            decision["combined_p_ai"],
            label["variant"],
            "classified",
        ),
    )
    details = {
        "creator_id": creator_id,
        "text_excerpt": _excerpt(text),
        "verdict": decision["verdict"],
        "confidence": decision["confidence"],
        "combined_p_ai": decision["combined_p_ai"],
        "signals": decision["signals"],
        "label": label["text"],
        "label_variant": label["variant"],
    }
    conn.execute(
        """INSERT INTO audit_log (content_id, event_type, timestamp, details)
           VALUES (?, 'submission', ?, ?)""",
        (content_id, ts, json.dumps(details)),
    )
    conn.commit()
    conn.close()


def get_submission(content_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_appeal(content_id, reason):
    """Flip status to under_review and log the appeal next to the original
    decision. Returns the updated submission row, or None if not found."""
    submission = get_submission(content_id)
    if submission is None:
        return None

    conn = get_conn()
    ts = _now()
    conn.execute(
        "UPDATE submissions SET status = 'under_review' WHERE content_id = ?",
        (content_id,),
    )
    details = {
        "creator_id": submission["creator_id"],
        "appeal_reasoning": reason,
        "original_verdict": submission["verdict"],
        "original_confidence": submission["confidence"],
        "original_label_variant": submission["label_variant"],
        "new_status": "under_review",
    }
    conn.execute(
        """INSERT INTO audit_log (content_id, event_type, timestamp, details)
           VALUES (?, 'appeal', ?, ?)""",
        (content_id, ts, json.dumps(details)),
    )
    conn.commit()
    conn.close()

    submission["status"] = "under_review"
    return submission


def get_log(limit=100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY entry_id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "entry_id": r["entry_id"],
                "content_id": r["content_id"],
                "event_type": r["event_type"],
                "timestamp": r["timestamp"],
                "details": json.loads(r["details"]),
            }
        )
    return out

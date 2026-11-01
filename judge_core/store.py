"""SQLite storage: audit log of every decision, plus the human review queue and its labels."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, policy_id TEXT, input_hash TEXT,
  decision TEXT, decided_by TEXT, path TEXT, tiers TEXT, latency_ms REAL, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY, ts REAL, policy_id TEXT, input_hash TEXT, state TEXT, question TEXT,
  context TEXT, status TEXT DEFAULT 'pending', label TEXT, labeled_at REAL,
  UNIQUE (policy_id, input_hash)
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- audit ---
    def log_decision(self, policy_id: str, input_hash: str, decision: Any, decided_by: str,
                     path: list[str], tiers: list[dict], latency_ms: float, cost_usd: float) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions (ts, policy_id, input_hash, decision, decided_by, path, tiers, latency_ms, cost_usd)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), policy_id, input_hash, json.dumps(decision, ensure_ascii=False), decided_by,
             json.dumps(path), json.dumps(tiers, ensure_ascii=False), latency_ms, cost_usd))
        self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def decisions(self, policy_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM decisions", ()
        if policy_id:
            sql, args = sql + " WHERE policy_id = ?", (policy_id,)
        rows = self.db.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(r), "decision": json.loads(r["decision"]), "path": json.loads(r["path"]),
                 "tiers": json.loads(r["tiers"])} for r in rows]

    # --- human queue ---
    def enqueue(self, policy_id: str, input_hash: str, state: Any, question: dict, context: dict) -> int:
        self.db.execute(
            "INSERT OR IGNORE INTO review_queue (ts, policy_id, input_hash, state, question, context) VALUES (?,?,?,?,?,?)",
            (time.time(), policy_id, input_hash, json.dumps(state, ensure_ascii=False),
             json.dumps(question, ensure_ascii=False), json.dumps(context, ensure_ascii=False)))
        self.db.commit()
        row = self.db.execute("SELECT id FROM review_queue WHERE policy_id=? AND input_hash=?",
                              (policy_id, input_hash)).fetchone()
        return row["id"]

    def pending(self, policy_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM review_queue WHERE status='pending'", ()
        if policy_id:
            sql, args = sql + " AND policy_id = ?", (policy_id,)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY id", args).fetchall()]

    def label(self, item_id: int, label: Any) -> None:
        self.db.execute("UPDATE review_queue SET status='labeled', label=?, labeled_at=? WHERE id=?",
                        (json.dumps(label, ensure_ascii=False), time.time(), item_id))
        self.db.commit()

    def lookup_label(self, policy_id: str, input_hash: str) -> tuple[bool, Any]:
        row = self.db.execute("SELECT label FROM review_queue WHERE policy_id=? AND input_hash=? AND status='labeled'",
                              (policy_id, input_hash)).fetchone()
        return (True, json.loads(row["label"])) if row else (False, None)

    def export_labels(self, policy_id: str | None = None) -> list[dict]:
        """Labeled items as training/calibration data: {policy_id, state, label}."""
        sql, args = "SELECT policy_id, state, label FROM review_queue WHERE status='labeled'", ()
        if policy_id:
            sql, args = sql + " AND policy_id = ?", (policy_id,)
        return [{"policy_id": r["policy_id"], "state": json.loads(r["state"]), "label": json.loads(r["label"])}
                for r in self.db.execute(sql, args).fetchall()]

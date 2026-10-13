"""SQLite journal and durable workflow checkpoints; one local server process."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from triageflow.schemas import Run


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL, checkpoint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    type TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, project_id: str, issue: str, mode: str) -> Run:
        stamp = now()
        run = Run(
            id=uuid4().hex,
            project_id=project_id,
            issue=issue,
            mode=mode,
            created_at=stamp,
            updated_at=stamp,
        )
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                (run.id, run.model_dump_json(), json.dumps({"next": "planner"})),
            )
        return run

    def load(self, run_id: str) -> tuple[Run, dict]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return Run.model_validate_json(row["document"]), json.loads(row["checkpoint"])

    def save(self, run: Run, checkpoint: dict):
        run.updated_at = now()
        with self.connection() as conn:
            conn.execute(
                "UPDATE runs SET document=?, checkpoint=? WHERE id=?",
                (run.model_dump_json(), json.dumps(checkpoint), run.id),
            )

    def claim(self, run_id: str) -> tuple[Run, dict] | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = Run.model_validate_json(row["document"])
            if run.status != "queued":
                return None
            run.status = "running"
            run.updated_at = now()
            conn.execute("UPDATE runs SET document=? WHERE id=?", (run.model_dump_json(), run_id))
            return run, json.loads(row["checkpoint"])

    def requeue(self, run_id: str) -> Run:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = Run.model_validate_json(row["document"])
            if run.status not in {"failed", "interrupted"}:
                raise ValueError("Only failed or interrupted runs can resume.")
            run.status, run.error, run.updated_at = "queued", None, now()
            conn.execute("UPDATE runs SET document=? WHERE id=?", (run.model_dump_json(), run_id))
        self.event(run_id, "resumed", run.stage, "从最近检查点继续，保留已用预算。")
        return run

    def recover(self):
        # Called only once at server startup. Multi-worker deployment is unsupported.
        for run in self.list(limit=None):
            if run.status in {"running", "queued"}:
                run, checkpoint = self.load(run.id)
                run.status = "interrupted"
                run.error = "进程中断；可从最近检查点恢复。"
                self.save(run, checkpoint)

    def list(self, limit: int | None = 50) -> list[Run]:
        query = "SELECT document FROM runs ORDER BY rowid DESC"
        with self.connection() as conn:
            rows = conn.execute(
                query + (" LIMIT ?" if limit is not None else ""),
                (limit,) if limit is not None else (),
            ).fetchall()
        return [Run.model_validate_json(row["document"]) for row in rows]

    def event(self, run_id: str, kind: str, stage: str, message: str, data: dict | None = None):
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO events (run_id,type,stage,message,data,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, kind, stage, message, json.dumps(data or {}), now()),
            )

    def events(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

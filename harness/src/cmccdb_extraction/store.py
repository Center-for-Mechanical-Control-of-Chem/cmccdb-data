"""Transactional task state and append-only audit events in a local SQLite store."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .schema import canonical


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "harness.sqlite3"
        with self.connect() as con:
            con.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, manifest TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS plans (run TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS experiments (
                run TEXT, key TEXT, reaction_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(run,key));
              CREATE TABLE IF NOT EXISTS tasks (
                run TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(run,id));
              CREATE TABLE IF NOT EXISTS assignments (
                run TEXT, task TEXT, worker TEXT, model TEXT NOT NULL, kind TEXT NOT NULL,
                token TEXT NOT NULL, expires REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                payload TEXT, PRIMARY KEY(run,task,worker));
              CREATE TABLE IF NOT EXISTS claims (
                run TEXT, id TEXT PRIMARY KEY, task TEXT, worker TEXT, experiment TEXT,
                path TEXT NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS decisions (
                run TEXT, experiment TEXT, path TEXT, data TEXT NOT NULL,
                PRIMARY KEY(run,experiment,path));
              CREATE TABLE IF NOT EXISTS chemistry_reviews (
                run TEXT, experiment TEXT, data TEXT NOT NULL, PRIMARY KEY(run,experiment));
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, time REAL NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS claims_field ON claims(run,experiment,path);
              CREATE INDEX IF NOT EXISTS events_run_sequence ON events(run,seq);
            """)

    @contextmanager
    def connect(self, write=False):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def event(con, run, kind, data):
        con.execute("INSERT INTO events(run,time,kind,data) VALUES(?,?,?,?)",
                    (run, time.time(), kind, canonical(data)))

    def manifest(self, run):
        with self.connect() as con:
            row = con.execute("SELECT manifest FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise ValueError("Unknown run ID")
        return json.loads(row[0])

    def run_dir(self, run):
        self.manifest(run)  # Validate IDs against state rather than accepting paths.
        return self.root / run

    def audit(self, run):
        manifest = self.manifest(run)
        with self.connect() as con:
            data = {name: [dict(r) for r in con.execute(f"SELECT * FROM {name} WHERE run=?", (run,))]
                    for name in ["experiments", "tasks", "assignments", "claims", "decisions", "chemistry_reviews", "events"]}
        for rows in data.values():
            for row in rows:
                for key in ["data", "payload"]:
                    if key in row and row[key] is not None:
                        row[key] = json.loads(row[key])
                row.pop("token", None)  # Lease tokens are operational credentials.
        return dict(audit_version=1, manifest=manifest, **data)

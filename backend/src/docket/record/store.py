"""The SQLite store (RFC 10.6). Plane 4 only: nothing outside record/ writes here.

Assumptions written down as RFC section 0 rule 2 asks:
  * RFC 10.6 names the tables, the columns and the required functions but not
    their signatures. The signatures below are the simplest ones that serve the
    routes in RFC section 11.
  * The table list is taken literally, so `run_status` keeps exactly its four
    columns; `get_status()` reads `change_key` out of `runs` when the run has
    already been saved, and returns None for it while a run is still collecting.
  * RFC 11 says `simulated` is true "when any section of the record has
    provenance = simulated". A nested fragment (verify.rehearsal) is part of its
    section, so the flag is computed by walking the whole record for any
    `provenance` field equal to "simulated".
  * Timestamps are stored as ISO-8601 strings in UTC so ordering is lexical.
  * `docket.collectors.otlp` is imported lazily, inside the two functions that
    need `FlatEvent`, so plane 4 never pulls plane 1 in at import time. A stored
    event is duck-typed on the way in (`.to_row()`, else `.name/.timestamp/.attrs`)
    and rebuilt with `FlatEvent.from_row` on the way out.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from docket.models.fragments import EditEvent
from docket.models.manifest import ManifestSnapshot
from docket.models.run import Run

log = logging.getLogger("docket.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS otel_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    attrs_json  TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_otel_events_session ON otel_events(session_id);

CREATE TABLE IF NOT EXISTS edit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    tool_use_id TEXT,
    ts          TEXT NOT NULL,
    event_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_edit_events_session ON edit_events(session_id);

CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY,
    repo_root  TEXT,
    cwd        TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    modes_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    change_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    decision   TEXT NOT NULL,
    composite  REAL NOT NULL,
    run_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_change_key ON runs(change_key);

CREATE TABLE IF NOT EXISTS run_status (
    run_id     TEXT PRIMARY KEY,
    stage      TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    change_key      TEXT PRIMARY KEY,
    freshservice_id INTEGER,
    url             TEXT
);

CREATE TABLE IF NOT EXISTS manifest_ledger (
    agent_id      TEXT NOT NULL,
    sequence      INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (agent_id, sequence)
);

CREATE TABLE IF NOT EXISTS coverage_posted (
    head_sha      TEXT PRIMARY KEY,
    coverage_json TEXT NOT NULL,
    received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_overrides (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""

STAGES = ("collecting", "correlating", "deciding", "recording", "done", "error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(ts: datetime | str | None) -> str:
    if ts is None:
        return _now()
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        out = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


_FLAT_EVENT: Any = None


def _flat_event_cls() -> Any:
    """`otlp.FlatEvent`, imported lazily. None while plane 1 is unavailable."""
    global _FLAT_EVENT
    if _FLAT_EVENT is None:
        try:
            from docket.collectors.otlp import FlatEvent

            _FLAT_EVENT = FlatEvent
        except Exception as exc:                          # plane 1 not importable yet
            log.debug("otlp.FlatEvent unavailable, using stored rows: %s", exc)
            _FLAT_EVENT = False
    return _FLAT_EVENT or None


class StoredEvent:
    """What `load_session_parts` yields when `otlp.FlatEvent` cannot be imported.

    Carries the same three attributes the Claude Code adapter reads, and stays
    subscriptable so either shape can be consumed.
    """

    __slots__ = ("name", "timestamp", "attrs")

    def __init__(self, name: str, timestamp: Any, attrs: dict) -> None:
        self.name = name
        self.timestamp = timestamp
        self.attrs = attrs

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __repr__(self) -> str:
        return f"StoredEvent(name={self.name!r}, timestamp={self.timestamp!r})"


def _has_simulated(node: Any) -> bool:
    """True when any `provenance` anywhere in the record is "simulated" (RFC 11)."""
    if isinstance(node, dict):
        if node.get("provenance") == "simulated":
            return True
        return any(_has_simulated(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_simulated(v) for v in node)
    return False


class Store:
    """One SQLite file, opened once, written under one lock."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path not in (":memory:", "") and not self.db_path.startswith("file:"):
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(Path(self.db_path).expanduser())
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---- plumbing ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            self.conn.commit()
            return cur

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(args)).fetchall())

    def _row(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(args)).fetchone()

    # ---- plane 1 raw tables ----------------------------------------------

    def save_event(self, event: Any, name: Any = None, ts: Any = None, attrs: Any = None) -> int:
        """Store one flattened OTLP event.

        Takes an `otlp.FlatEvent` (or anything with `.name`, `.timestamp`,
        `.attrs`), or the explicit `(session_id, name, ts, attrs)` row.
        """
        if name is not None or ts is not None or attrs is not None:
            row_session = str(event or "_none")
            return self._insert_event(
                row_session, str(name or ""), _iso(ts),
                json.dumps(dict(attrs or {}), ensure_ascii=False, default=str),
            )
        if hasattr(event, "to_row"):
            row_session, name, ts_iso, attrs_json = event.to_row()
        else:
            event_attrs = dict(getattr(event, "attrs", None) or {})
            row_session = str(event_attrs.get("session.id") or "_none")
            name = getattr(event, "name", "") or ""
            ts_iso = _iso(getattr(event, "timestamp", None))
            attrs_json = json.dumps(event_attrs, ensure_ascii=False, default=str)
        return self._insert_event(row_session, name, _iso(ts_iso), attrs_json)

    def _insert_event(self, session_id: str, name: str, ts_iso: str, attrs_json: str) -> int:
        cur = self._write(
            "INSERT INTO otel_events (session_id, name, ts, attrs_json, received_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id or "_none", name, ts_iso, attrs_json, _now()),
        )
        return int(cur.lastrowid or 0)

    def save_edit_event(self, event: EditEvent) -> int:
        cur = self._write(
            "INSERT INTO edit_events (session_id, tool_use_id, ts, event_json) VALUES (?, ?, ?, ?)",
            (event.session_id, event.tool_use_id, _iso(event.timestamp), event.model_dump_json()),
        )
        return int(cur.lastrowid or 0)

    def upsert_session_meta(
        self,
        session_id: str,
        meta: dict | None = None,
        seen_at: datetime | str | None = None,
        **fields: Any,
    ) -> None:
        """`cwd`, `repo_root`, `permission_mode` and `seen_at`, as a dict or as keywords."""
        meta = dict(meta or {})
        meta.update({k: v for k, v in fields.items() if v is not None})
        repo_root = meta.get("repo_root")
        cwd = meta.get("cwd")
        mode = meta.get("permission_mode") or meta.get("mode")
        stamp = _iso(seen_at if seen_at is not None else meta.get("seen_at"))
        with self._lock:
            row = self._row("SELECT * FROM session_meta WHERE session_id = ?", (session_id,))
            if row is None:
                modes = [mode] if mode else []
                self._write(
                    "INSERT INTO session_meta (session_id, repo_root, cwd, first_seen, last_seen, modes_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, repo_root, cwd, stamp, stamp, json.dumps(modes)),
                )
                return
            modes = json.loads(row["modes_json"] or "[]")
            if mode and (not modes or modes[-1] != mode):
                modes.append(mode)
            self._write(
                "UPDATE session_meta SET repo_root = ?, cwd = ?, last_seen = ?, modes_json = ?"
                " WHERE session_id = ?",
                (repo_root or row["repo_root"], cwd or row["cwd"], max(stamp, row["last_seen"] or stamp),
                 json.dumps(modes), session_id),
            )

    def session_meta(self, session_id: str) -> dict:
        row = self._row("SELECT * FROM session_meta WHERE session_id = ?", (session_id,))
        if row is None:
            return {}
        return {
            "session_id": row["session_id"],
            "repo_root": row["repo_root"],
            "cwd": row["cwd"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "modes": json.loads(row["modes_json"] or "[]"),
        }

    def _session_rows(self) -> list[dict]:
        events = {
            r["session_id"]: r
            for r in self._rows(
                "SELECT session_id, COUNT(*) AS events, MIN(ts) AS started_at, MAX(ts) AS ended_at"
                " FROM otel_events WHERE session_id != '_none' GROUP BY session_id"
            )
        }
        edits = {
            r["session_id"]: r
            for r in self._rows(
                "SELECT session_id, COUNT(*) AS edits, MIN(ts) AS started_at, MAX(ts) AS ended_at"
                " FROM edit_events GROUP BY session_id"
            )
        }
        meta = {r["session_id"]: r for r in self._rows("SELECT * FROM session_meta")}
        out: list[dict] = []
        for sid in sorted(set(events) | set(edits) | set(meta)):
            e, d, m = events.get(sid), edits.get(sid), meta.get(sid)
            starts = [x["started_at"] for x in (e, d) if x and x["started_at"]]
            ends = [x["ended_at"] for x in (e, d) if x and x["ended_at"]]
            if m and m["first_seen"]:
                starts.append(m["first_seen"])
            if m and m["last_seen"]:
                ends.append(m["last_seen"])
            out.append({
                "session_id": sid,
                "started_at": min(starts) if starts else None,
                "ended_at": max(ends) if ends else None,
                "events": int(e["events"]) if e else 0,
                "edits": int(d["edits"]) if d else 0,
                "repo_url": self._repo_url(sid),
            })
        out.sort(key=lambda r: (r["started_at"] or "", r["session_id"]), reverse=True)
        return out

    def _repo_url(self, session_id: str) -> str | None:
        row = self._row(
            "SELECT attrs_json FROM otel_events WHERE session_id = ?"
            " AND attrs_json LIKE '%vcs.repository.url.full%' ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if row is None:
            return None
        try:
            return json.loads(row["attrs_json"]).get("vcs.repository.url.full")
        except (ValueError, AttributeError):
            return None

    def list_sessions(self) -> list[dict]:
        """Every session seen, newest first (RFC 11 `GET /sessions`)."""
        return self._session_rows()

    def sessions_between(self, start: datetime | str, end: datetime | str) -> list[str]:
        """The ids of the sessions whose activity overlaps [start, end], newest first."""
        lo, hi = _parse(_iso(start)), _parse(_iso(end))
        out: list[str] = []
        for row in self._session_rows():
            s, e = _parse(row["started_at"]), _parse(row["ended_at"])
            if s is None and e is None:
                continue
            s = s or e
            e = e or s
            if lo and e < lo:
                continue
            if hi and s > hi:
                continue
            out.append(row["session_id"])
        return out

    def load_session_parts(self, session_id: str) -> tuple[list[Any], list[EditEvent], dict]:
        """(flat events, edit events, session meta) — what the adapter's build_session wants.

        The events come back as `otlp.FlatEvent` when plane 1 is importable, and
        as `StoredEvent` (same `.name` / `.timestamp` / `.attrs`) when it is not.
        """
        flat_event = _flat_event_cls()
        events: list[Any] = []
        for row in self._rows(
            "SELECT name, ts, attrs_json FROM otel_events WHERE session_id = ? ORDER BY ts, id",
            (session_id,),
        ):
            if flat_event is not None:
                events.append(flat_event.from_row(
                    session_id, row["name"], row["ts"], row["attrs_json"]
                ))
                continue
            try:
                attrs = json.loads(row["attrs_json"])
            except ValueError:
                attrs = {}
            events.append(StoredEvent(row["name"], row["ts"], attrs))
        edits: list[EditEvent] = []
        for row in self._rows(
            "SELECT event_json FROM edit_events WHERE session_id = ? ORDER BY ts, id", (session_id,)
        ):
            try:
                edits.append(EditEvent.model_validate_json(row["event_json"]))
            except Exception as exc:                      # a stored row we can no longer parse
                log.debug("skipping unparseable edit_event for %s: %s", session_id, exc)
        return events, edits, self.session_meta(session_id)

    # ---- coverage ---------------------------------------------------------

    def save_coverage(self, head_sha: str, coverage_json: Any) -> None:
        """Coverage pushed from CI (RFC 7.4). Accepts JSON text or a plain object."""
        body = coverage_json if isinstance(coverage_json, str) else json.dumps(
            coverage_json, ensure_ascii=False, default=str
        )
        self._write(
            "INSERT INTO coverage_posted (head_sha, coverage_json, received_at) VALUES (?, ?, ?)"
            " ON CONFLICT(head_sha) DO UPDATE SET coverage_json = excluded.coverage_json,"
            " received_at = excluded.received_at",
            (head_sha, body, _now()),
        )

    def posted_coverage(self, head_sha: str) -> dict | None:
        row = self._row("SELECT coverage_json FROM coverage_posted WHERE head_sha = ?", (head_sha,))
        if row is None:
            return None
        try:
            return json.loads(row["coverage_json"])
        except ValueError:
            return None

    # the name this had before the plane-4 integration contract settled
    coverage_for = posted_coverage

    # ---- runs -------------------------------------------------------------

    def save_run(self, run: Run) -> None:
        self._write(
            "INSERT INTO runs (run_id, change_key, created_at, kind, decision, composite, run_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET change_key = excluded.change_key,"
            " created_at = excluded.created_at, kind = excluded.kind, decision = excluded.decision,"
            " composite = excluded.composite, run_json = excluded.run_json",
            (run.run_id, run.change_key, _iso(run.created_at), run.record.kind,
             run.decision.decision, float(run.decision.composite), run.model_dump_json()),
        )

    def get_run(self, run_id: str) -> Run | None:
        row = self._row("SELECT run_json FROM runs WHERE run_id = ?", (run_id,))
        return Run.model_validate_json(row["run_json"]) if row else None

    def latest_run(self, change_key: str) -> Run | None:
        row = self._row(
            "SELECT run_json FROM runs WHERE change_key = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (change_key,),
        )
        return Run.model_validate_json(row["run_json"]) if row else None

    def runs_for(self, change_key: str) -> list[dict]:
        """Run summaries for one change, newest first."""
        rows = self._rows(
            "SELECT run_id, created_at, kind, decision, composite, run_json FROM runs"
            " WHERE change_key = ? ORDER BY created_at DESC, rowid DESC",
            (change_key,),
        )
        out = []
        for row in rows:
            try:
                data = json.loads(row["run_json"])
            except ValueError:
                data = {}
            out.append({
                "run_id": row["run_id"],
                "change_key": change_key,
                "created_at": row["created_at"],
                "kind": row["kind"],
                "decision": row["decision"],
                "composite": row["composite"],
                "mode": data.get("mode"),
                "seal": data.get("seal", ""),
            })
        return out

    def latest_runs(self) -> list[Run]:
        """The latest run per change_key, newest first."""
        seen: set[str] = set()
        out: list[Run] = []
        for row in self._rows(
            "SELECT change_key, run_json FROM runs ORDER BY created_at DESC, rowid DESC"
        ):
            key = row["change_key"]
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(Run.model_validate_json(row["run_json"]))
            except Exception as exc:
                log.debug("skipping unparseable run for %s: %s", key, exc)
        return out

    def list_changes(self) -> list[dict]:
        """The queue: latest run per change_key, newest first (RFC 11 `GET /changes`)."""
        tickets = {r["change_key"]: r for r in self._rows("SELECT * FROM tickets")}
        rows: list[dict] = []
        for run in self.latest_runs():
            record = run.record
            ticket = tickets.get(run.change_key)
            fid = run.outputs.freshservice_change_id
            if fid is None and ticket is not None:
                fid = ticket["freshservice_id"]
            rows.append({
                "change_key": run.change_key,
                "title": record.title,
                "kind": record.kind,
                "decision": run.decision.decision,
                "composite": run.decision.composite,
                "risk_level": run.decision.risk_level,
                "join_confidence": record.join.confidence,
                "missing_links": run.chain.missing_count,
                "filed_by_docket": record.kind != "code",
                "freshservice_id": fid,
                "updated_at": _iso(run.created_at),
                "simulated": _has_simulated(record.model_dump(mode="json")),
                # RFC 12 asks the threshold slider to preview "HOLD if composite > value
                # OR any hard stop fired", so the row has to carry the hard stops. The
                # rest of the row is exactly the field list in RFC 11.
                "hard_stops": [h.id for h in run.decision.hard_stops_fired],
            })
        return rows

    def reset_runs(self) -> None:
        """`POST /demo/reset`: drop runs, their progress and their tickets.

        Collected evidence (otel_events, edit_events, session_meta) and the
        manifest ledger are deliberately kept.
        """
        with self._lock:
            for table in ("runs", "run_status", "tickets"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()

    # ---- progress ---------------------------------------------------------

    def set_status(self, run_id: str, stage: str, message: str = "") -> None:
        if stage not in STAGES:
            log.debug("unknown stage %r for run %s", stage, run_id)
        self._write(
            "INSERT INTO run_status (run_id, stage, message, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET stage = excluded.stage,"
            " message = excluded.message, updated_at = excluded.updated_at",
            (run_id, stage, message or "", _now()),
        )

    def get_status(self, run_id: str) -> dict | None:
        row = self._row("SELECT * FROM run_status WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        run_row = self._row("SELECT change_key FROM runs WHERE run_id = ?", (run_id,))
        return {
            "run_id": row["run_id"],
            "stage": row["stage"],
            "message": row["message"],
            "updated_at": row["updated_at"],
            "change_key": run_row["change_key"] if run_row else None,
        }

    # ---- tickets ----------------------------------------------------------

    def ticket_for(self, change_key: str) -> dict | None:
        row = self._row("SELECT * FROM tickets WHERE change_key = ?", (change_key,))
        if row is None:
            return None
        return {
            "change_key": row["change_key"],
            "freshservice_id": row["freshservice_id"],
            "url": row["url"],
        }

    def save_ticket(self, change_key: str, freshservice_id: int | None, url: str | None = None) -> None:
        self._write(
            "INSERT INTO tickets (change_key, freshservice_id, url) VALUES (?, ?, ?)"
            " ON CONFLICT(change_key) DO UPDATE SET freshservice_id = excluded.freshservice_id,"
            " url = excluded.url",
            (change_key, freshservice_id, url),
        )

    # ---- manifest ledger --------------------------------------------------

    def append_snapshot(self, snapshot: ManifestSnapshot) -> None:
        self._write(
            "INSERT INTO manifest_ledger (agent_id, sequence, manifest_hash, observed_at, snapshot_json)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(agent_id, sequence) DO UPDATE SET manifest_hash = excluded.manifest_hash,"
            " observed_at = excluded.observed_at, snapshot_json = excluded.snapshot_json",
            (snapshot.agent_id, int(snapshot.sequence), snapshot.manifest_hash,
             _iso(snapshot.observed_at), snapshot.model_dump_json()),
        )

    def latest_snapshot(self, agent_id: str) -> ManifestSnapshot | None:
        row = self._row(
            "SELECT snapshot_json FROM manifest_ledger WHERE agent_id = ?"
            " ORDER BY sequence DESC LIMIT 1",
            (agent_id,),
        )
        return ManifestSnapshot.model_validate_json(row["snapshot_json"]) if row else None

    def ledger(self, agent_id: str | None = None) -> list[ManifestSnapshot]:
        """Snapshot history, newest first. Every agent when agent_id is None."""
        if agent_id:
            rows = self._rows(
                "SELECT snapshot_json FROM manifest_ledger WHERE agent_id = ?"
                " ORDER BY sequence DESC",
                (agent_id,),
            )
        else:
            rows = self._rows(
                "SELECT snapshot_json FROM manifest_ledger ORDER BY observed_at DESC, sequence DESC"
            )
        out = []
        for row in rows:
            try:
                out.append(ManifestSnapshot.model_validate_json(row["snapshot_json"]))
            except Exception as exc:
                log.debug("skipping unparseable snapshot: %s", exc)
        return out

    # ---- overrides --------------------------------------------------------

    def get_override(self, key: str, default: Any = None) -> Any:
        row = self._row("SELECT value_json FROM settings_overrides WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def set_override(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO settings_overrides (key, value_json) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )

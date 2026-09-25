"""OTLP/HTTP JSON flattening, shared by every adapter (RFC 7.1.2).

Assumptions taken here (RFC section 0, rule 2 — simplest reading, written down):
  * `timeUnixNano` may arrive as a string or an int; both are accepted.
  * When a record carries neither `event.timestamp` nor `timeUnixNano`, we fall
    back to `observedTimeUnixNano`, and then to the Unix epoch. A missing
    timestamp is a value, not an exception (rule 6).
  * A malformed resource/scope/record is skipped and logged at DEBUG. The rest
    of the payload is kept (rule 5 / RFC 7.1.1 "never return 5xx").
"""

from __future__ import annotations

import gzip
import json
import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("docket.collectors.otlp")

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NAME_PREFIX = "claude_code."


@dataclass
class FlatEvent:
    """One OTLP log record, flattened. `attrs` are plain Python values."""

    name: str
    timestamp: datetime
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "timestamp": self.timestamp.isoformat(),
            "attrs": self.attrs,
        }

    def to_row(self) -> tuple[str, str, str, str]:
        """`(session_id, name, ts_iso, attrs_json)` — what the store stores.

        `session_id` is the literal `"_none"` when the event carries none.
        """
        return (
            session_id_of(self),
            self.name,
            self.timestamp.isoformat(),
            json.dumps(self.attrs, ensure_ascii=False, default=str),
        )

    @classmethod
    def from_row(
        cls,
        session_id: str | None = None,
        name: str | None = None,
        ts_iso: Any = None,
        attrs_json: Any = None,
    ) -> "FlatEvent":
        """Rebuild an event from a stored row. The inverse of `to_row`.

        `attrs_json` may be the JSON text the row holds or an already-decoded
        dict, because some stores decode it for the caller. A row that cannot
        be read yields an empty event rather than raising: stored evidence is
        kept, never thrown away.
        """
        attrs: dict[str, Any] = {}
        if isinstance(attrs_json, dict):
            attrs = dict(attrs_json)
        elif isinstance(attrs_json, (str, bytes, bytearray)) and attrs_json:
            try:
                decoded = json.loads(attrs_json)
                attrs = decoded if isinstance(decoded, dict) else {}
            except ValueError:
                log.debug("unparseable stored attrs_json, using {}")
        event = cls(
            name=name if isinstance(name, str) else "",
            timestamp=parse_iso(ts_iso) or from_unix_nano(ts_iso) or EPOCH,
            attrs=attrs,
        )
        # The stored session id is authoritative; keep it if the attrs lost it.
        if session_id and session_id != "_none" and "session.id" not in event.attrs:
            event.attrs["session.id"] = session_id
        return event

    @classmethod
    def from_dict(cls, row: Any) -> "FlatEvent":
        """The inverse of `to_dict`, for events read back out of the store.

        `timestamp` may be a datetime or an ISO 8601 string; anything else is
        read as the epoch, because a stored row is evidence we keep rather
        than throw away.
        """
        if isinstance(row, FlatEvent):
            return row
        if not isinstance(row, dict):
            return cls(name="", timestamp=EPOCH, attrs={})
        raw_ts = row.get("timestamp")
        if isinstance(raw_ts, datetime):
            ts = to_utc(raw_ts)
        else:
            ts = parse_iso(raw_ts) or from_unix_nano(raw_ts) or EPOCH
        attrs = row.get("attrs")
        name = row.get("name")
        return cls(
            name=name if isinstance(name, str) else "",
            timestamp=ts,
            attrs=dict(attrs) if isinstance(attrs, dict) else {},
        )


# --------------------------------------------------------------------------- #
# transport helpers
# --------------------------------------------------------------------------- #


def decode_body(body: bytes, content_encoding: str | None = None) -> bytes:
    """Undo `Content-Encoding` before JSON parsing (RFC 7.1.1).

    gzip and deflate are handled. Anything else (or an absent header) is
    returned untouched. A body that claims to be gzip but is not is returned
    as-is rather than raising: the JSON parse will report the real problem.
    """
    if not body:
        return b""
    enc = (content_encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip"):
        try:
            return gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            log.debug("body claimed gzip but did not decompress; using raw bytes")
            return body
    if enc == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            log.debug("body claimed deflate but did not decompress; using raw bytes")
            return body
    # Some exporters gzip without saying so.
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            return body
    return body


def parse_payload(body: bytes, content_encoding: str | None = None) -> dict:
    """decode_body + json.loads. Returns {} for anything unparseable."""
    try:
        text = decode_body(body, content_encoding).decode("utf-8", errors="replace")
        payload = json.loads(text) if text.strip() else {}
    except (ValueError, UnicodeDecodeError) as exc:
        log.debug("unparseable OTLP body: %s", exc)
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- #
# attribute conversion
# --------------------------------------------------------------------------- #


def _coerce_int(raw: Any) -> Any:
    """`intValue` often arrives as a JSON string. Keep the original on failure."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return raw


def _coerce_float(raw: Any) -> Any:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def _coerce_bool(raw: Any) -> Any:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return bool(raw)


def attr_value(value: Any) -> Any:
    """Convert one OTLP AnyValue to a plain Python value.

    Handles stringValue, intValue (often a string), doubleValue, boolValue,
    bytesValue, arrayValue.values[] and kvlistValue.values[]. An unknown shape
    returns None.
    """
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return _coerce_int(value["intValue"])
    if "doubleValue" in value:
        return _coerce_float(value["doubleValue"])
    if "boolValue" in value:
        return _coerce_bool(value["boolValue"])
    if "bytesValue" in value:
        return value["bytesValue"]
    if "arrayValue" in value:
        inner = value.get("arrayValue") or {}
        values = inner.get("values") if isinstance(inner, dict) else None
        return [attr_value(v) for v in values] if isinstance(values, list) else []
    if "kvlistValue" in value:
        inner = value.get("kvlistValue") or {}
        values = inner.get("values") if isinstance(inner, dict) else None
        return attributes_to_dict(values if isinstance(values, list) else [])
    if not value:
        # An explicitly empty AnyValue ({}) means "unset".
        return None
    log.debug("unknown OTLP AnyValue shape: %s", sorted(value))
    return None


def attributes_to_dict(attributes: Any) -> dict[str, Any]:
    """Convert an OTLP KeyValue list to a dict. Bad entries are skipped."""
    out: dict[str, Any] = {}
    if not isinstance(attributes, list):
        return out
    for item in attributes:
        if not isinstance(item, dict):
            log.debug("skipping non-object OTLP attribute: %r", item)
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            log.debug("skipping OTLP attribute with no key: %r", item)
            continue
        out[key] = attr_value(item.get("value"))
    return out


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #


def parse_iso(text: Any) -> datetime | None:
    """ISO 8601 -> tz-aware UTC datetime. Accepts a trailing Z. None on failure."""
    if isinstance(text, datetime):
        return to_utc(text)
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(raw))
    except ValueError:
        log.debug("unparseable ISO timestamp: %r", text)
        return None


def to_utc(dt: datetime) -> datetime:
    """A naive datetime is read as UTC; an aware one is converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_unix_nano(raw: Any) -> datetime | None:
    """`timeUnixNano` (string or int) -> tz-aware UTC datetime."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        nanos = int(str(raw).strip())
    except (TypeError, ValueError):
        log.debug("unparseable timeUnixNano: %r", raw)
        return None
    if nanos <= 0:
        return None
    try:
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        log.debug("out-of-range timeUnixNano: %r", raw)
        return None


def record_timestamp(record: dict, attrs: dict[str, Any]) -> datetime:
    """`attrs["event.timestamp"]` (ISO 8601) if present, else `timeUnixNano`."""
    ts = parse_iso(attrs.get("event.timestamp"))
    if ts is not None:
        return ts
    for key in ("timeUnixNano", "observedTimeUnixNano"):
        ts = from_unix_nano(record.get(key))
        if ts is not None:
            return ts
    return EPOCH


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #


def strip_prefix(name: str) -> str:
    """`claude_code.tool_result` -> `tool_result`."""
    return name[len(_NAME_PREFIX):] if name.startswith(_NAME_PREFIX) else name


def record_name(record: dict, attrs: dict[str, Any]) -> str:
    """First non-empty of attrs['event.name'], record['eventName'], body.stringValue."""
    candidates: list[Any] = [attrs.get("event.name"), record.get("eventName")]
    body = record.get("body")
    if isinstance(body, dict):
        candidates.append(attr_value(body))
    elif isinstance(body, str):
        candidates.append(body)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return strip_prefix(candidate.strip())
    return ""


# --------------------------------------------------------------------------- #
# flatten
# --------------------------------------------------------------------------- #


def flatten(payload: Any) -> list[FlatEvent]:
    """An OTLP/HTTP JSON logs payload -> a flat list of events (RFC 7.1.2).

    `attrs` merges resource attributes then record attributes (record wins).
    Scope attributes are merged in between, which is the simplest reading of
    "resource then record" for a payload that also carries a scope.
    """
    events: list[FlatEvent] = []
    if not isinstance(payload, dict):
        log.debug("OTLP payload is not an object: %r", type(payload))
        return events

    resource_logs = payload.get("resourceLogs")
    if not isinstance(resource_logs, list):
        # Tolerate the protobuf-ish snake_case spelling some exporters emit.
        resource_logs = payload.get("resource_logs")
    if not isinstance(resource_logs, list):
        return events

    for resource_log in resource_logs:
        if not isinstance(resource_log, dict):
            log.debug("skipping malformed resourceLogs entry")
            continue
        resource = resource_log.get("resource")
        resource_attrs = attributes_to_dict(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        scope_logs = resource_log.get("scopeLogs")
        if not isinstance(scope_logs, list):
            scope_logs = resource_log.get("scope_logs")
        if not isinstance(scope_logs, list):
            continue

        for scope_log in scope_logs:
            if not isinstance(scope_log, dict):
                log.debug("skipping malformed scopeLogs entry")
                continue
            scope = scope_log.get("scope")
            scope_attrs = attributes_to_dict(
                scope.get("attributes") if isinstance(scope, dict) else None
            )
            records = scope_log.get("logRecords")
            if not isinstance(records, list):
                records = scope_log.get("log_records")
            if not isinstance(records, list):
                continue

            for record in records:
                if not isinstance(record, dict):
                    log.debug("skipping malformed logRecord")
                    continue
                try:
                    attrs: dict[str, Any] = {}
                    attrs.update(resource_attrs)
                    attrs.update(scope_attrs)
                    attrs.update(attributes_to_dict(record.get("attributes")))
                    events.append(
                        FlatEvent(
                            name=record_name(record, attrs),
                            timestamp=record_timestamp(record, attrs),
                            attrs=attrs,
                        )
                    )
                except Exception as exc:   # never 5xx on one bad record
                    log.debug("skipping unflattenable logRecord: %s", exc)
    return events


def session_id_of(event: FlatEvent) -> str:
    """The store key. Events without a `session.id` live under `"_none"`."""
    sid = event.attrs.get("session.id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    if sid is not None and not isinstance(sid, (dict, list)):
        return str(sid)
    return "_none"


def service_name_of(event: FlatEvent) -> str:
    """The resource attribute that picks an adapter (RFC 7.1.4)."""
    name = event.attrs.get("service.name")
    return name.strip() if isinstance(name, str) and name.strip() else ""

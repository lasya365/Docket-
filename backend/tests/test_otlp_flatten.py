"""RFC 15.3: the payload in 7.1.2, plus intValue as a string, plus gzip."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

from docket.collectors.otlp import (
    FlatEvent,
    attr_value,
    decode_body,
    flatten,
    parse_payload,
    record_name,
    session_id_of,
    service_name_of,
)

# The exact payload from RFC 7.1.2.
RFC_PAYLOAD = {
    "resourceLogs": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}}
                ]
            },
            "scopeLogs": [
                {
                    "logRecords": [
                        {
                            "timeUnixNano": "1758700000000000000",
                            "body": {"stringValue": "claude_code.tool_result"},
                            "attributes": [
                                {"key": "session.id", "value": {"stringValue": "abc"}}
                            ],
                        }
                    ]
                }
            ],
        }
    ]
}


def test_rfc_payload_flattens():
    events = flatten(RFC_PAYLOAD)
    assert len(events) == 1
    ev = events[0]
    assert ev.name == "tool_result"                       # claude_code. prefix stripped
    assert ev.attrs["session.id"] == "abc"
    assert ev.attrs["service.name"] == "claude-code"      # resource attrs merged in
    assert ev.timestamp == datetime(2025, 9, 24, 7, 46, 40, tzinfo=timezone.utc)
    assert ev.timestamp.tzinfo is not None
    assert session_id_of(ev) == "abc"
    assert service_name_of(ev) == "claude-code"


def test_int_value_arriving_as_a_string():
    assert attr_value({"intValue": "42"}) == 42
    assert attr_value({"intValue": 42}) == 42
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1758700000000000000",
                                "body": {"stringValue": "claude_code.user_prompt"},
                                "attributes": [
                                    {"key": "event.sequence", "value": {"intValue": "7"}},
                                    {"key": "prompt_length", "value": {"intValue": 120}},
                                    {"key": "duration_ms", "value": {"doubleValue": 12.5}},
                                    {"key": "success", "value": {"boolValue": True}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    ev = flatten(payload)[0]
    assert ev.attrs["event.sequence"] == 7
    assert isinstance(ev.attrs["event.sequence"], int)
    assert ev.attrs["prompt_length"] == 120
    assert ev.attrs["duration_ms"] == 12.5
    assert ev.attrs["success"] is True


def test_gzip_body_is_decoded():
    raw = json.dumps(RFC_PAYLOAD).encode("utf-8")
    body = gzip.compress(raw)
    assert decode_body(body, "gzip") == raw
    assert decode_body(raw, None) == raw
    # header casing and the x-gzip spelling
    assert decode_body(body, "GZIP") == raw
    assert decode_body(body, "x-gzip") == raw
    events = flatten(parse_payload(body, "gzip"))
    assert [e.name for e in events] == ["tool_result"]


def test_gzip_claim_that_is_not_gzip_does_not_raise():
    raw = json.dumps(RFC_PAYLOAD).encode("utf-8")
    assert decode_body(raw, "gzip") == raw          # returned untouched, no 5xx
    assert flatten(parse_payload(raw, "gzip"))[0].name == "tool_result"


def test_array_and_kvlist_values():
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1758700000000000000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "claude_code.api_request"}},
                                    {
                                        "key": "tags",
                                        "value": {
                                            "arrayValue": {
                                                "values": [
                                                    {"stringValue": "a"},
                                                    {"intValue": "2"},
                                                ]
                                            }
                                        },
                                    },
                                    {
                                        "key": "meta",
                                        "value": {
                                            "kvlistValue": {
                                                "values": [
                                                    {"key": "k", "value": {"boolValue": "false"}}
                                                ]
                                            }
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    ev = flatten(payload)[0]
    assert ev.name == "api_request"
    assert ev.attrs["tags"] == ["a", 2]
    assert ev.attrs["meta"] == {"k": False}


def test_event_name_resolution_order():
    attrs = {"event.name": "claude_code.tool_result"}
    assert record_name({"eventName": "other", "body": {"stringValue": "x"}}, attrs) == "tool_result"
    assert record_name({"eventName": "claude_code.api_error", "body": {"stringValue": "x"}}, {}) == "api_error"
    assert record_name({"body": {"stringValue": "claude_code.user_prompt"}}, {}) == "user_prompt"
    assert record_name({}, {}) == ""


def test_iso_event_timestamp_wins_over_time_unix_nano():
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1758700000000000000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "user_prompt"}},
                                    {"key": "event.timestamp", "value": {"stringValue": "2026-01-02T03:04:05Z"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    ev = flatten(payload)[0]
    assert ev.timestamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_malformed_records_are_skipped_not_raised():
    payload = {
        "resourceLogs": [
            "not-an-object",
            {
                "resource": {"attributes": "nonsense"},
                "scopeLogs": [
                    {"logRecords": ["nope", {"attributes": [{"novalue": 1}, {"key": "session.id", "value": {"stringValue": "s1"}}]}]},
                    "also-not-an-object",
                ],
            },
        ]
    }
    events = flatten(payload)
    assert len(events) == 1
    assert events[0].attrs == {"session.id": "s1"}
    assert events[0].name == ""

    assert flatten(None) == []
    assert flatten({}) == []
    assert parse_payload(b"{not json", None) == {}


def test_events_without_a_session_id_go_under_none():
    ev = FlatEvent(name="tool_result", timestamp=datetime.now(timezone.utc), attrs={})
    assert session_id_of(ev) == "_none"


def test_to_row_and_from_row_round_trip():
    ev = flatten(RFC_PAYLOAD)[0]
    session_id, name, ts_iso, attrs_json = ev.to_row()
    assert session_id == "abc"
    assert name == "tool_result"
    assert ts_iso == "2025-09-24T07:46:40+00:00"
    assert json.loads(attrs_json) == ev.attrs

    rebuilt = FlatEvent.from_row(session_id, name, ts_iso, attrs_json)
    assert rebuilt.name == ev.name
    assert rebuilt.timestamp == ev.timestamp
    assert rebuilt.attrs == ev.attrs


def test_to_row_uses_none_for_an_event_with_no_session():
    ev = FlatEvent(name="api_request", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), attrs={})
    assert ev.to_row()[0] == "_none"


def test_from_row_is_tolerant_of_stored_junk():
    # attrs already decoded by the store.
    assert FlatEvent.from_row("s1", "tool_result", "2026-01-01T00:00:00Z", {"k": "v"}).attrs["k"] == "v"
    # unparseable attrs, unparseable timestamp, missing name
    bad = FlatEvent.from_row("s1", None, "not-a-time", "{not json")
    assert bad.name == "" and bad.attrs == {"session.id": "s1"} and bad.timestamp.year == 1970
    # the stored session id is restored when the attrs lost it
    assert FlatEvent.from_row("s7", "x", None, "{}").attrs["session.id"] == "s7"
    # "_none" is not written back as a real session id
    assert "session.id" not in FlatEvent.from_row("_none", "x", None, "{}").attrs
    # a nanosecond timestamp still works
    assert FlatEvent.from_row("s1", "x", "1758700000000000000", "{}").timestamp.year == 2025

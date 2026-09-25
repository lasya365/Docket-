"""The Freshservice writer (RFC 10.3). Plane 4 only.

Assumptions written down as RFC section 0 rule 2 asks:
  * The dry-run file holds one JSON document per run:
    {"method", "url", "change_key", "run_id", "payload", "note"} so that the
    change payload and the note that follows it are both visible. The change
    payload itself is verbatim what a live POST/PUT would send, under "payload".
  * `change_impact` is the blast-radius signal's summary; that is the only signal
    that describes impact (RFC 9.4).
  * Every string that reaches Freshservice is passed through html.escape,
    including the four planning fields, because Freshservice renders them as HTML
    (RFC section 0 rule 10).
  * `sync_approval` maps the numeric `approval_status` through
    config.freshservice.approval_status_map; an unmapped code is "unknown".
  * With freshservice.enabled false nothing is fetched, so sync_approval leaves
    the approval as it found it.
  * The description is a full report, not only the RFC 10.3 minimum, so a CAB
    member can judge the change from the Freshservice ticket alone: the RFC's
    required parts (decision, signal table, chain with MISSING in bold, join,
    hard stops, seal, link) plus intent, evidence, files, sessions, review,
    verification, rollout, backout and the advisory brief. The planning fields
    carry formatted HTML as well. Every value is still escaped.
  * A custom field whose configured name is empty is not sent, so an account
    without the Docket fields can still file changes (set the name to "").
  * Links are only rendered for http(s) URLs; anything else is shown as text.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from docket.models.run import Run, RunOutputs

log = logging.getLogger("docket.record.freshservice")

DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NO_INTENT = "No stated intent. Filed by Docket."
ADVISORY_HEADING = "Advisory, written by AI. Not part of the decision."
APPROVAL_VALUES = {"none", "requested", "approved", "rejected", "unknown"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        out = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(DATE_FORMAT)


# ---- HTML helpers: every value that goes in is escaped here ----------------

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _text(value: str | None) -> str:
    """Escaped text with its line breaks kept."""
    return _e((value or "").strip()).replace("\r\n", "\n").replace("\n", "<br>")


def _link(url: str | None, label: str | None = None) -> str:
    if not url:
        return _e(label or "")
    if not url.startswith(("https://", "http://")):
        return _e(label or url)
    return f'<a href="{_e(url)}" target="_blank" rel="noopener">{_e(label or url)}</a>'


def _row(label: str, value_html: str) -> str:
    return f"<tr><td><b>{_e(label)}</b></td><td>{value_html}</td></tr>"


def _humanize(value: str | None) -> str:
    return (value or "").replace("_", " ").strip().capitalize()


def _clip(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


TABLE = '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
DECISION_COLORS = {"APPROVE": "#067647", "HOLD": "#b54708", "REJECTED": "#b42318"}
MAX_FILES = 50          # rows in the files table; the rest are counted, not listed
MAX_EXCERPT = 400       # characters of one prompt excerpt


class FreshserviceWriter:
    """Upserts one Freshservice change per change_key and posts the evidence note."""

    def __init__(
        self,
        settings: Any,
        client: Any = None,
        clock: Callable[[], datetime] | None = None,
        store: Any = None,
        status_writer: Any = None,
        out_dir: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.config
        self.fs = settings.config.freshservice
        self.store = store                         # optional: enables a true upsert
        self.client = client                       # an httpx.Client, injected
        self.clock = clock or _utcnow              # injected so dates are deterministic
        self.status_writer = status_writer         # GithubStatusWriter, for approval sync
        self.out_dir = Path(out_dir) if out_dir else settings.path("out/freshservice")

    # ---- plumbing ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.fs.enabled)

    @property
    def base_url(self) -> str:
        return f"https://{self.fs.domain}/api/v2"

    def _auth(self) -> tuple[str, str]:
        """HTTP basic: username = API key, password = X. Never logged (RFC 17.5)."""
        return (self.settings.freshservice_api_key, "X")

    def _client(self) -> Any:
        if self.client is None:
            import httpx                            # only reached when enabled

            self.client = httpx.Client(timeout=30.0)
        return self.client

    def change_url(self, change_id: int) -> str:
        return f"https://{self.fs.domain}/a/changes/{change_id}"

    def docket_url(self, change_key: str) -> str:
        base = self.config.server.public_base_url.rstrip("/")
        return f"{base}/#/changes/{change_key}"

    def _error(self, run: Run, message: str) -> None:
        log.error(message)
        run.outputs.errors.append(message)

    def _request(self, method: str, path: str, payload: dict | None, run: Run) -> dict | None:
        url = f"{self.base_url}{path}"
        try:
            response = self._client().request(
                method, url, json=payload, auth=self._auth(),
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            self._error(run, f"freshservice {method} {path} failed: {exc.__class__.__name__}: {exc}")
            return None
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            body = getattr(response, "text", "")
            # RFC 10.3: log the response body in full, the message names the field.
            self._error(run, f"freshservice {method} {path} -> {status}: {body}")
            return None
        try:
            return response.json()
        except Exception:
            return {}

    # ---- the payload ------------------------------------------------------

    def planned_dates(self, run: Run) -> tuple[str, str]:
        start = _parse_datetime(run.record.deploy.window)
        if start is None:
            start = self.clock() + timedelta(hours=self.fs.planned_start_offset_hours)
        end = start + timedelta(hours=self.fs.planned_window_hours)
        return _stamp(start), _stamp(end)

    def _signal(self, run: Run, name: str):
        for signal in run.signals:
            if signal.signal == name:
                return signal
        return None

    def build_payload(self, run: Run) -> dict:
        record, decision = run.record, run.decision
        defaults = self.fs.defaults
        fields = self.fs.custom_fields
        start, end = self.planned_dates(run)
        payload = {
            "subject": f"[Docket] {record.title}".strip(),
            "description": self.build_description(run),
            "requester_id": self.fs.requester_id,
            "priority": defaults.priority,
            "impact": defaults.impact,
            "status": defaults.status,
            "change_type": defaults.change_type,
            "risk": self.fs.risk_codes.get(decision.risk_level),
            "planned_start_date": start,
            "planned_end_date": end,
            "planning_fields": {
                "reason_for_change": {"description": self.reason_html(run)},
                "change_impact": {"description": self.impact_html(run)},
                "rollout_plan": {"description": self.rollout_html(run)},
                "backout_plan": {"description": self.backout_html(run)},
            },
        }
        values = {
            fields.decision: decision.decision,
            fields.score: round(float(decision.composite), 2),
            fields.seal: run.seal,
            fields.source_url: record.source_url or self.docket_url(run.change_key),
        }
        custom = {name: value for name, value in values.items() if name}
        if custom:
            payload["custom_fields"] = custom
        return payload

    # ---- planning fields --------------------------------------------------

    def reason_html(self, run: Run) -> str:
        intent = run.record.intent
        text = (intent.text or "").strip() if intent.present else ""
        if not text:
            return _e(NO_INTENT)
        out = []
        if intent.ref or intent.url:
            out.append(f"<p><b>Requested in:</b> {_link(intent.url, intent.ref or intent.url)}"
                       f" ({_e(intent.source or 'ticket')})</p>")
        out.append(f"<p>{_text(text)}</p>")
        if intent.scope_paths:
            scope = ", ".join(f"<code>{_e(p)}</code>" for p in intent.scope_paths)
            out.append(f"<p><b>Declared scope:</b> {scope}</p>")
        return "".join(out)

    def impact_html(self, run: Run) -> str:
        decision, diff = run.decision, run.record.diff
        blast = self._signal(run, "blast_radius")
        scope = self._signal(run, "unattributed")
        out = [
            f"<p><b>Risk level:</b> {_e(_humanize(decision.risk_level))} "
            f"(Docket score {decision.composite:.2f} against a threshold of {decision.threshold:g})</p>",
        ]
        if blast:
            out.append(f"<p><b>Blast radius:</b> {_e(blast.summary)}</p>")
        if diff.present:
            adds = sum(f.additions for f in diff.files)
            dels = sum(f.deletions for f in diff.files)
            ai = diff.totals.get("ai", 0) + diff.totals.get("mixed", 0)
            out.append(f"<p><b>Code touched:</b> {len(diff.files)} file(s), +{adds} / -{dels} lines, "
                       f"{ai} written by an AI agent.</p>")
        if scope and scope.status == "computed":
            out.append(f"<p><b>Scope:</b> {_e(scope.summary)}</p>")
        return "".join(out)

    def rollout_html(self, run: Run) -> str:
        out = [f"<p>{_text(run.rollout_text)}</p>"] if run.rollout_text else []
        gate = {
            "APPROVE": "Docket approved this change. It may proceed in the planned window.",
            "HOLD": "Docket put this change on HOLD. Do not roll out until the CAB approves it.",
            "REJECTED": "The CAB rejected this change. Do not roll it out.",
        }.get(run.decision.decision)
        if gate:
            out.append(f"<p><b>Gate:</b> {_e(gate)}</p>")
        return "".join(out)

    def backout_html(self, run: Run) -> str:
        plan = run.backout
        if not plan.steps and not plan.summary:
            return f"<p>{_text(plan.text)}</p>"
        out = [f"<p><b>{_e(plan.summary)}</b></p>"] if plan.summary else []
        if plan.steps:
            out.append("<ol>")
            for step in plan.steps:
                out.append(f"<li><b>{_e(step.key)}:</b> {_e(step.from_value)} &rarr; {_e(step.to_value)}</li>")
            out.append("</ol>")
        if plan.naive_plan:
            out.append(f"<p><b>Naive plan:</b> {_text(plan.naive_plan)}</p>")
        if plan.why_naive_fails:
            out.append(f"<p><b>Why that is not enough:</b> {_text(plan.why_naive_fails)}</p>")
        if not plan.available:
            out.append("<p><b>No automatic backout is available.</b></p>")
        return "".join(out)

    # ---- the description --------------------------------------------------

    def build_description(self, run: Run) -> str:
        """HTML built from escaped text (RFC 10.3, RFC section 0 rule 10)."""
        return "".join([
            self._verdict_html(run),
            self._summary_html(run),
            self._intent_html(run),
            self._signals_html(run),
            self._evidence_html(run),
            self._diff_html(run),
            self._sessions_html(run),
            self._review_html(run),
            self._chain_html(run),
            self._plans_html(run),
            self._advisory_html(run),
            self._integrity_html(run),
        ])

    def _verdict_html(self, run: Run) -> str:
        decision, chain = run.decision, run.chain
        color = DECISION_COLORS.get(decision.decision, "#101828")
        present = sum(1 for link in chain.links if link.present)
        out = [
            f'<h2>Docket decision: <span style="color:{color}">{_e(decision.decision)}</span></h2>',
            TABLE,
            _row("Decision", f'<b style="color:{color}">{_e(decision.decision)}</b>'),
            _row("Risk score", f"<b>{decision.composite:.2f}</b> against a threshold of "
                               f"<b>{_e(f'{decision.threshold:g}')}</b>"),
            _row("Risk level", _e(_humanize(decision.risk_level))),
            _row("Evidence links", f"{present} of {len(chain.links)} present"
                                   + (f", <b>{chain.missing_count} MISSING</b>" if chain.missing_count else "")),
            _row("Join", f"Join: method <b>{_e(run.record.join.method)}</b>, confidence "
                         f"<b>{_e(run.record.join.confidence)}</b>"),
            "</table>",
        ]
        if decision.hard_stops_fired:
            out.append("<p><b>Hard stops fired</b> (each forces HOLD on its own):</p><ul>")
            for stop in decision.hard_stops_fired:
                out.append(f"<li><b>{_e(stop.id)}:</b> {_e(stop.reason)}</li>")
            out.append("</ul>")
        if decision.excluded_signals:
            names = ", ".join(_e(_humanize(s)) for s in decision.excluded_signals)
            out.append(f"<p><b>Left out of the score</b> (no evidence to compute them): {names}</p>")
        return "".join(out)

    def _summary_html(self, run: Run) -> str:
        record = run.record
        return "".join([
            "<h3>1. The change</h3>", TABLE,
            _row("Title", _e(record.title)),
            _row("Kind", _e(_humanize(record.kind))),
            _row("Source", _link(record.source_url) or "none"),
            _row("Change key", f"<code>{_e(run.change_key)}</code>"),
            _row("Opened", _e(_stamp(record.created_at))),
            _row("Scored by Docket", f"{_e(_stamp(run.created_at))} (run <code>{_e(run.run_id)}</code>)"),
            _row("Docket record", _link(self.docket_url(run.change_key))),
            "</table>",
        ])

    def _intent_html(self, run: Run) -> str:
        intent = run.record.intent
        if not intent.present:
            return "<h3>2. Why: the stated intent</h3><p><b>MISSING.</b> No ticket or issue states why this change was made.</p>"
        return "<h3>2. Why: the stated intent</h3>" + self.reason_html(run) + self._provenance(intent.provenance)

    def _signals_html(self, run: Run) -> str:
        out = [
            "<h3>3. Risk signals</h3>",
            "<p>Each signal scores 0 to 100; higher is riskier. The weighted average is the risk score.</p>",
            TABLE,
            "<tr><th>Signal</th><th>Score</th><th>Status</th><th>Headline</th><th>What it found</th></tr>",
        ]
        for signal in run.signals:
            out.append(
                f"<tr><td><b>{_e(signal.label)}</b></td><td>{_e(f'{signal.score:.2f}')}</td>"
                f"<td>{_e(_humanize(signal.status))}</td><td>{_e(signal.headline)}</td>"
                f"<td>{_e(signal.summary)}</td></tr>"
            )
        out.append("</table>")
        return "".join(out)

    def _evidence_html(self, run: Run) -> str:
        out = ["<h3>4. Evidence behind each signal</h3>"]
        for signal in run.signals:
            out.append(f"<p><b>{_e(signal.label)}</b></p>")
            if not signal.evidence:
                out.append("<p>No evidence items.</p>")
                continue
            out.append("<ul>")
            for item in signal.evidence:
                out.append(f"<li>{_e(item.text)}{self._provenance(item.provenance)}</li>")
            out.append("</ul>")
        return "".join(out)

    def _diff_html(self, run: Run) -> str:
        diff = run.record.diff
        out = ["<h3>5. Code changes and who wrote them</h3>"]
        if not diff.present:
            out.append("<p><b>MISSING.</b> No diff was found for this change.</p>")
            return "".join(out)
        t = diff.totals
        out.append(
            f"<p><b>Added lines by author:</b> AI agent <b>{t.get('ai', 0)}</b> · "
            f"AI then edited by a human <b>{t.get('mixed', 0)}</b> · human <b>{t.get('human', 0)}</b> · "
            f"unknown <b>{t.get('unknown', 0)}</b>{self._provenance(diff.provenance)}</p>"
        )
        if diff.files:
            out.append(TABLE)
            out.append("<tr><th>File</th><th>Status</th><th>Added</th><th>Removed</th></tr>")
            for f in diff.files[:MAX_FILES]:
                out.append(f"<tr><td><code>{_e(f.path)}</code></td><td>{_e(f.status)}</td>"
                           f"<td>+{f.additions}</td><td>-{f.deletions}</td></tr>")
            out.append("</table>")
            if len(diff.files) > MAX_FILES:
                out.append(f"<p>…and {len(diff.files) - MAX_FILES} more file(s).</p>")
        return "".join(out)

    def _sessions_html(self, run: Run) -> str:
        sessions = run.record.sessions
        out = ["<h3>6. AI agent sessions</h3>"]
        if not sessions:
            out.append("<p>No agent session is linked to this change.</p>")
            return "".join(out)
        for s in sessions:
            if not s.present:
                out.append(f"<p><b>Session <code>{_e(s.session_id)}</code>: seen but not joined.</b> "
                           "Its telemetry never reached Docket, so its lines cannot be attributed.</p>")
                continue
            out.append(f"<p><b>Session <code>{_e(s.session_id)}</code></b>{self._provenance(s.provenance)}</p>")
            out.append(TABLE)
            out.append(_row("Agent / model", f"{_e(s.agent or 'unknown')} / {_e(s.model or 'unknown')}"))
            out.append(_row("Turns", f"{s.turns} (retries {s.retries})"))
            out.append(_row("Linked by", f"{_e(s.join_method)}, confidence <b>{_e(s.join_confidence)}</b>"))
            if s.matched_commits:
                out.append(_row("Commits", ", ".join(f"<code>{_e(c[:10])}</code>" for c in s.matched_commits)))
            if s.files_written:
                out.append(_row("Files written", "<br>".join(f"<code>{_e(p)}</code>" for p in s.files_written)))
            out.append(_row("Lines written", str(s.lines_written)))
            if s.tools_used:
                names = sorted({t.display_name for t in s.tools_used})
                out.append(_row("Tools used", _e(", ".join(names))))
            out.append("</table>")
            if s.prompt_excerpts:
                out.append("<p><b>What the agent was asked:</b></p><ul>")
                for excerpt in s.prompt_excerpts:
                    out.append(f"<li><i>{_text(_clip(excerpt, MAX_EXCERPT))}</i></li>")
                out.append("</ul>")
        for note in run.record.join.notes:
            out.append(f"<p>{_e(note)}</p>")
        return "".join(out)

    def _review_html(self, run: Run) -> str:
        review, verify = run.record.review, run.record.verify
        out = ["<h3>7. Human review and testing</h3>", TABLE]
        if not review.present:
            out.append(_row("Review", "<b>MISSING.</b> No pull request exists."))
        elif review.approvers:
            names = ", ".join(f"{_e(a.reviewer)} at <code>{_e(a.approved_commit[:10])}</code>" for a in review.approvers)
            out.append(_row("Approved by", names + (" <b>(stale: new commits since)</b>" if review.stale_approval else "")))
        else:
            out.append(_row("Approved by", "<b>Nobody.</b>"))
        if review.present:
            out.append(_row("Review comments", f"{review.comments} on {len(review.files_commented)} of "
                                               f"{review.files_changed} changed file(s)"))
        out.append(_row("CI status", _e(verify.ci_status) if verify.present else "<b>MISSING</b>"))
        out.append(_row("Tests written by", _e(verify.tests_authored_by)))
        out.append(_row("Line coverage", f"{len(verify.coverage_by_line)} file(s) reported"
                        if verify.coverage_by_line else "No coverage report reached Docket"))
        if verify.rehearsal is not None:
            out.append(_row("Rehearsal", "present" + self._provenance(verify.rehearsal.provenance)))
        out.append("</table>")
        return "".join(out)

    def _chain_html(self, run: Run) -> str:
        out = ["<h3>8. Evidence chain: request to release</h3>", TABLE,
               "<tr><th>Link</th><th>Found</th><th>Summary</th></tr>"]
        for link in run.chain.links:
            found = "yes" if link.present else "<b>MISSING</b>"
            detail = f"<br><small>{_e(link.detail)}</small>" if link.detail else ""
            out.append(f"<tr><td><b>{_e(_humanize(link.name))}</b></td><td>{found}</td>"
                       f"<td>{_e(link.summary)}{detail}</td></tr>")
        out.append("</table>")
        return "".join(out)

    def _plans_html(self, run: Run) -> str:
        return ("<h3>9. Rollout and backout</h3>"
                "<p><b>Rollout</b></p>" + self.rollout_html(run)
                + "<p><b>Backout</b></p>" + self.backout_html(run))

    def _advisory_html(self, run: Run) -> str:
        out = [f"<h3>10. {_e(ADVISORY_HEADING)}</h3>"]
        if not run.advisory:
            out.append("<p>No advisory was produced for this change.</p>")
            return "".join(out)
        for item in run.advisory:
            label = "Brief" if item.kind == "brief" else _humanize(item.kind)
            source = f" ({_e(item.model)})" if item.model else ""
            out.append(f"<p><b>{_e(label)}</b>{source}: {_text(item.text)}</p>")
        return "".join(out)

    def _integrity_html(self, run: Run) -> str:
        url = self.docket_url(run.change_key)
        return "".join([
            "<h3>11. Integrity</h3>", TABLE,
            _row("Seal", f"Seal: <code>{_e(run.seal)}</code>"),
            _row("Config hash", f"<code>{_e(run.decision.config_hash)}</code>"),
            _row("Verify", f"Recompute the seal at {_link(url)}"),
            "</table>",
            "<p><small>Values marked <b>(simulated)</b> were not read from a real system. "
            "The decision is arithmetic over the evidence above; the advisory text never changes it.</small></p>",
        ])

    @staticmethod
    def _provenance(provenance: str | None) -> str:
        return " <b><i>(simulated)</i></b>" if provenance == "simulated" else ""

    def build_note(self, run: Run) -> str:
        """The evidence list and the board brief, all escaped."""
        e = html.escape
        out: list[str] = ["<h4>Evidence</h4><ul>"]
        for signal in run.signals:
            for item in signal.evidence:
                out.append(
                    f"<li>[{e(item.id)}] {e(item.text)}"
                    f"{' <i>(simulated)</i>' if item.provenance == 'simulated' else ''}</li>"
                )
        out.append("</ul>")
        out.append(f"<h4>{e(ADVISORY_HEADING)}</h4>")
        if run.advisory:
            out.append("<ul>")
            for item in run.advisory:
                refs = f" ({e(', '.join(item.evidence_refs))})" if item.evidence_refs else ""
                out.append(f"<li>{e(item.kind)}: {e(item.text)}{refs}</li>")
            out.append("</ul>")
        else:
            out.append("<p>No advisory was produced for this change.</p>")
        return "".join(out)

    # ---- the writer -------------------------------------------------------

    def write(self, run: Run, store: Any = None) -> RunOutputs:
        """Upsert the change and post the note. Mutates run.outputs, never raises.

        Returns run.outputs for convenience; a caller that expects None is safe.
        """
        store = store or self.store
        try:
            payload = self.build_payload(run)
            note = {"body": self.build_note(run)}
            ticket = store.ticket_for(run.change_key) if store else None
            existing = ticket.get("freshservice_id") if ticket else None
            if not existing:
                # no store to remember the id: fall back to what the run carries
                existing = run.outputs.freshservice_change_id or None
            method = "PUT" if existing else "POST"
            path = f"/changes/{existing}" if existing else "/changes"

            if not self.enabled:
                self._dry_run(run, method, path, payload, note)
                return run.outputs

            data = self._request(method, path, payload, run)
            if data is None:
                return run.outputs
            change_id = existing or self._extract_id(data)
            if change_id is None:
                self._error(run, f"freshservice {method} {path} returned no change id")
                return run.outputs
            run.outputs.freshservice_change_id = int(change_id)
            run.outputs.freshservice_url = self.change_url(int(change_id))
            if store:
                store.save_ticket(run.change_key, int(change_id), run.outputs.freshservice_url)
            self._request("POST", f"/changes/{change_id}/notes", note, run)
            return run.outputs
        except Exception as exc:                        # never fail the run
            self._error(run, f"freshservice writer failed: {exc.__class__.__name__}: {exc}")
            return run.outputs

    @staticmethod
    def _extract_id(data: dict) -> int | None:
        change = data.get("change") if isinstance(data, dict) else None
        if isinstance(change, dict) and change.get("id") is not None:
            return int(change["id"])
        if isinstance(data, dict) and data.get("id") is not None:
            return int(data["id"])
        return None

    def _dry_run(self, run: Run, method: str, path: str, payload: dict, note: dict) -> None:
        """RFC 10.3: write the payload to out/freshservice and return a fake id of 0."""
        document = {
            "method": method,
            "url": f"{self.base_url}{path}",
            "change_key": run.change_key,
            "run_id": run.run_id,
            "payload": payload,
            "note": note,
        }
        target = self.dry_run_path(run)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        run.outputs.freshservice_change_id = 0
        log.info("freshservice dry run written to %s", target)

    def dry_run_path(self, run: Run) -> Path:
        return self.out_dir / f"{run.change_key}-{run.run_id}.json"

    # ---- approval sync ----------------------------------------------------

    def sync_approval(self, change_key: str, store: Any = None) -> RunOutputs | None:
        """Pull `approval_status` back from Freshservice (RFC 10.3). Never raises.

        Returns None only when there is no run to update.
        """
        store = store or self.store
        if store is None:
            return None
        run = store.latest_run(change_key)
        if run is None:
            return None
        try:
            ticket = store.ticket_for(change_key)
            change_id = ticket.get("freshservice_id") if ticket else None
            if not self.enabled or not change_id:
                return run.outputs
            data = self._request("GET", f"/changes/{change_id}", None, run)
            approval = "unknown"
            if data:
                change = data.get("change", data)
                code = change.get("approval_status") if isinstance(change, dict) else None
                for name, codes in self.fs.approval_status_map.items():
                    if code in codes and name in APPROVAL_VALUES:
                        approval = name
                        break
            run.outputs.freshservice_approval = approval
            store.save_run(run)
            if self.status_writer is not None and approval in ("approved", "rejected"):
                self.status_writer.write(run, approval=approval)
                store.save_run(run)
            return run.outputs
        except Exception as exc:
            self._error(run, f"freshservice approval sync failed: {exc.__class__.__name__}: {exc}")
            return run.outputs

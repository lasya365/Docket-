"""CI Collector: CI status + per-line coverage -> CoverageFragment (RFC 7.4).

Every client is injected, so no test touches the network (rule 8). This module
reads no environment variables: the caller passes `simulate` (which it derives
from `DOCKET_SIMULATE_COVERAGE=1`) and, in live mode, leaves it False.

Assumptions taken here (RFC section 0, rule 2 — simplest reading, written down):
  * Order of preference: posted coverage passed in by the caller, then a CI
    artifact, then the simulated fallback. Posted coverage is a deliberate act
    by a CI step, so it beats an artifact Docket had to go looking for.
  * The CI status is collected even when no coverage is found; an unreachable
    status is `"unknown"`, never an exception.
  * A combined status of `"pending"` with no statuses at all falls back to
    check runs, per RFC 7.4 step 1.
  * `provenance` follows `source`: "simulated" for the fallback, "real"
    otherwise. A fragment with `source = "none"` has `present = False`.
  * The simulated fallback needs the added lines of the diff. When the caller
    passes none, there is nothing to simulate and the fragment is absent.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from typing import Any, Iterable, Mapping

from docket.models.fragments import ChangedFile, CoverageFragment, FileCoverage

log = logging.getLogger("docket.collectors.ci_coverage")

COVERAGE_JSON_NAME = "coverage.json"
SIMULATED_COVERED_PERCENT = 60          # RFC 7.4 step 4

# coverage.py JSON: {"files": {"<path>": {"executed_lines": [...], "missing_lines": [...]}}}
K_FILES = "files"
K_EXECUTED = "executed_lines"
K_MISSING = "missing_lines"

_STATE_MAP = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "pending": "pending",
}
_CONCLUSION_MAP = {
    "success": "success",
    "neutral": "success",
    "skipped": "success",
    "failure": "failure",
    "timed_out": "failure",
    "cancelled": "failure",
    "action_required": "failure",
    "stale": "failure",
}


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def _ints(values: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(values, (list, tuple)):
        return out
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            out.append(value)
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            out.append(int(value.strip()))
        else:
            log.debug("skipping non-integer coverage line %r", value)
    return out


def parse_coverage_json(payload: Any) -> dict[str, FileCoverage]:
    """coverage.py JSON -> `{path: FileCoverage}`. Anything unreadable gives {}."""
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            log.debug("coverage payload was not JSON: %s", exc)
            return {}
    if not isinstance(payload, Mapping):
        return {}
    files = payload.get(K_FILES, payload)
    if not isinstance(files, Mapping):
        return {}

    out: dict[str, FileCoverage] = {}
    for path, entry in files.items():
        if not isinstance(path, str) or not isinstance(entry, Mapping):
            log.debug("skipping malformed coverage entry for %r", path)
            continue
        out[path] = FileCoverage(
            executed=_ints(entry.get(K_EXECUTED)),
            missing=_ints(entry.get(K_MISSING)),
        )
    return out


def added_lines_of(files: Iterable[ChangedFile] | None) -> dict[str, list[int]]:
    """The added line numbers of a RepoFragment's files, for the simulator."""
    out: dict[str, list[int]] = {}
    for f in files or []:
        numbers = [line.line_no for hunk in f.hunks for line in hunk.added]
        if numbers:
            out[f.path] = numbers
    return out


def simulate_coverage(added_lines: Mapping[str, Iterable[int]] | None) -> dict[str, FileCoverage]:
    """Deterministic fallback (RFC 7.4 step 4).

    `covered = int(sha256(f"{path}:{line_no}").hexdigest(), 16) % 100 < 60`.
    """
    out: dict[str, FileCoverage] = {}
    for path, numbers in (added_lines or {}).items():
        executed: list[int] = []
        missing: list[int] = []
        for line_no in numbers:
            digest = hashlib.sha256(f"{path}:{line_no}".encode("utf-8")).hexdigest()
            if int(digest, 16) % 100 < SIMULATED_COVERED_PERCENT:
                executed.append(line_no)
            else:
                missing.append(line_no)
        out[path] = FileCoverage(executed=executed, missing=missing)
    return out


def coverage_from_zip(blob: bytes) -> dict[str, FileCoverage]:
    """Read `coverage.json` out of a downloaded artifact zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == COVERAGE_JSON_NAME]
            if not names:
                names = [n for n in archive.namelist() if n.endswith(".json")]
            if not names:
                log.debug("coverage artifact held no JSON file")
                return {}
            return parse_coverage_json(archive.read(names[0]))
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        log.debug("unreadable coverage artifact: %s", exc)
        return {}


# --------------------------------------------------------------------------- #
# the collector
# --------------------------------------------------------------------------- #


class CoverageCollector:
    """`collect(repo, head_sha, posted_coverage=None) -> CoverageFragment`.

    `github_client` is a PyGithub client, `http_client` an httpx-style client
    with `.get(url, headers=..., follow_redirects=...)`. Both are injected and
    both may be None: the fragment then reports what it could not find.
    """

    def __init__(
        self,
        github_client: Any = None,
        token: str = "",
        artifact_name: str = "coverage-json",
        http_client: Any = None,
    ):
        self._client = github_client
        self._token = token
        self._artifact_name = artifact_name
        self._http = http_client

    # -- CI status ---------------------------------------------------------- #

    def ci_status(self, repo: str, head_sha: str) -> str:
        """`success | failure | pending | unknown` (RFC 7.4 step 1)."""
        if self._client is None or not head_sha:
            return "unknown"
        try:
            repo_obj = self._client.get_repo(repo)
            commit = repo_obj.get_commit(head_sha)
        except Exception as exc:
            log.debug("no commit for %s@%s: %s", repo, head_sha, exc)
            return "unknown"

        total = None
        state = ""
        try:
            combined = commit.get_combined_status()
            state = str(getattr(combined, "state", "") or "").lower()
            total = getattr(combined, "total_count", None)
            if total is None:
                statuses = getattr(combined, "statuses", None)
                total = len(list(statuses)) if statuses is not None else None
        except Exception as exc:
            log.debug("no combined status for %s: %s", head_sha, exc)

        if state and not (total == 0):
            mapped = _STATE_MAP.get(state)
            if mapped:
                return mapped

        # No statuses: fall back to check runs.
        try:
            runs = list(commit.get_check_runs())
        except Exception as exc:
            log.debug("no check runs for %s: %s", head_sha, exc)
            return _STATE_MAP.get(state, "unknown")

        if not runs:
            return _STATE_MAP.get(state, "unknown")
        conclusions: list[str] = []
        for run in runs:
            if str(getattr(run, "status", "") or "").lower() != "completed":
                return "pending"
            conclusions.append(str(getattr(run, "conclusion", "") or "").lower())
        mapped = [_CONCLUSION_MAP.get(c, "unknown") for c in conclusions]
        if "failure" in mapped:
            return "failure"
        if all(m == "success" for m in mapped):
            return "success"
        return "unknown"

    # -- real coverage from an Actions artifact ----------------------------- #

    def artifact_coverage(self, repo: str, head_sha: str) -> dict[str, FileCoverage]:
        if self._client is None or self._http is None or not head_sha:
            return {}
        try:
            repo_obj = self._client.get_repo(repo)
            runs = list(repo_obj.get_workflow_runs(head_sha=head_sha))
        except Exception as exc:
            log.debug("no workflow runs for %s: %s", head_sha, exc)
            return {}

        for run in runs:
            try:
                artifacts = list(run.get_artifacts())
            except Exception as exc:
                log.debug("no artifacts on a workflow run: %s", exc)
                continue
            for artifact in artifacts:
                if str(getattr(artifact, "name", "") or "") != self._artifact_name:
                    continue
                url = getattr(artifact, "archive_download_url", None)
                if not url:
                    continue
                blob = self._download(url)
                if not blob:
                    continue
                files = coverage_from_zip(blob)
                if files:
                    return files
        return {}

    def _download(self, url: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = self._http.get(url, headers=headers, follow_redirects=True)
            status = getattr(response, "status_code", 200)
            if int(status) >= 400:
                log.debug("coverage artifact download returned %s", status)
                return b""
            return getattr(response, "content", b"") or b""
        except Exception as exc:
            log.debug("coverage artifact download failed: %s", exc)
            return b""

    # -- the one public entry point ----------------------------------------- #

    def collect(
        self,
        repo: str,
        head_sha: str,
        posted: Any = None,
        simulate: bool = False,
        added_lines: Mapping[str, Iterable[int]] | None = None,
    ) -> CoverageFragment:
        """Assemble a CoverageFragment. Never raises; an empty result is a value.

        `added_lines` is path -> added line numbers, which the simulated
        fallback in RFC 7.4 step 4 needs; the pipeline passes it from the
        RepoFragment (see `added_lines_of`).
        """
        status = self.ci_status(repo, head_sha)

        files = parse_coverage_json(posted) if posted else {}
        source = "posted" if files else "none"

        if not files:
            files = self.artifact_coverage(repo, head_sha)
            if files:
                source = "github-artifact"

        if not files and simulate:
            files = simulate_coverage(added_lines)
            if files:
                source = "simulated"

        present = bool(files)
        if not present:
            source = "none"
        provenance = "simulated" if source == "simulated" else "real"

        return CoverageFragment(
            head_sha=head_sha,
            ci_status=status,
            files=files,
            present=present,
            source=source,
            provenance=provenance,
        )

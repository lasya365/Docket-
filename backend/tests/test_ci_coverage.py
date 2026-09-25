"""CI Collector tests. Every client is faked: no test touches the network."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace as NS

import pytest

from docket.collectors.ci_coverage import (
    CoverageCollector,
    added_lines_of,
    coverage_from_zip,
    parse_coverage_json,
    simulate_coverage,
)
from docket.models.fragments import ChangedFile, DiffLine, Hunk

COVERAGE_PAYLOAD = {
    "files": {
        "provisioning/sync.py": {"executed_lines": [1, 2, 3], "missing_lines": [4, 5]},
        "provisioning/backoff.py": {"executed_lines": [10], "missing_lines": []},
    }
}
HEAD = "head-sha-1"


def zip_of(payload, name="coverage.json"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, json.dumps(payload))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeCommit:
    def __init__(self, state="success", total=1, check_runs=None, raise_status=False):
        self._state = state
        self._total = total
        self._check_runs = check_runs or []
        self._raise_status = raise_status

    def get_combined_status(self):
        if self._raise_status:
            raise RuntimeError("no statuses")
        return NS(state=self._state, total_count=self._total, statuses=[])

    def get_check_runs(self):
        return self._check_runs


_DEFAULT = object()


class FakeRepo:
    def __init__(self, commit=_DEFAULT, runs=None):
        self._commit = FakeCommit() if commit is _DEFAULT else commit
        self._runs = runs or []

    def get_commit(self, sha):
        if self._commit is None:
            raise RuntimeError("unknown sha")
        return self._commit

    def get_workflow_runs(self, head_sha=None):
        return self._runs


class FakeGithub:
    def __init__(self, repo=_DEFAULT):
        self.repo = FakeRepo() if repo is _DEFAULT else repo

    def get_repo(self, name):
        return self.repo


class FakeHttp:
    def __init__(self, content=b"", status_code=200, explode=False):
        self.content = content
        self.status_code = status_code
        self.explode = explode
        self.calls: list[tuple] = []

    def get(self, url, headers=None, follow_redirects=False):
        self.calls.append((url, headers, follow_redirects))
        if self.explode:
            raise RuntimeError("network is faked off")
        return NS(status_code=self.status_code, content=self.content)


def run_with_artifact(name="coverage-json", url="https://api/artifacts/1/zip"):
    artifact = NS(name=name, archive_download_url=url)
    return NS(get_artifacts=lambda: [artifact])


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_parse_coverage_json():
    files = parse_coverage_json(COVERAGE_PAYLOAD)
    assert files["provisioning/sync.py"].executed == [1, 2, 3]
    assert files["provisioning/sync.py"].missing == [4, 5]
    assert files["provisioning/backoff.py"].missing == []
    # The same payload as a JSON string.
    assert parse_coverage_json(json.dumps(COVERAGE_PAYLOAD)) == files
    # Junk is a missing section, not an exception.
    assert parse_coverage_json(None) == {}
    assert parse_coverage_json("{not json") == {}
    assert parse_coverage_json({"files": "nope"}) == {}
    assert parse_coverage_json({"files": {"a.py": "nope"}}) == {}
    # Line numbers that are not integers are dropped.
    weird = parse_coverage_json({"files": {"a.py": {"executed_lines": [1, "2", None, True]}}})
    assert weird["a.py"].executed == [1, 2]


def test_added_lines_of_reads_a_fragments_hunks():
    files = [
        ChangedFile(path="a.py", status="modified", hunks=[
            Hunk(hunk_id="a.py#0", path="a.py", old_start=1, old_lines=1, new_start=1, new_lines=2,
                 added=[DiffLine(line_no=2, text="x")], removed_count=0),
            Hunk(hunk_id="a.py#1", path="a.py", old_start=9, old_lines=1, new_start=9, new_lines=2,
                 added=[DiffLine(line_no=10, text="y")], removed_count=0),
        ]),
        ChangedFile(path="blob.bin", status="modified", patch_present=False),
    ]
    assert added_lines_of(files) == {"a.py": [2, 10]}
    assert added_lines_of(None) == {}


def test_simulated_coverage_matches_the_rfc_formula_and_is_deterministic():
    added = {"provisioning/sync.py": list(range(1, 51))}
    first = simulate_coverage(added)
    assert simulate_coverage(added) == first            # deterministic

    fc = first["provisioning/sync.py"]
    for line_no in range(1, 51):
        digest = hashlib.sha256(f"provisioning/sync.py:{line_no}".encode()).hexdigest()
        covered = int(digest, 16) % 100 < 60
        assert (line_no in fc.executed) is covered
        assert (line_no in fc.missing) is not covered
    assert sorted(fc.executed + fc.missing) == list(range(1, 51))


def test_coverage_from_zip():
    assert coverage_from_zip(zip_of(COVERAGE_PAYLOAD))["provisioning/sync.py"].executed == [1, 2, 3]
    # Nested path inside the zip.
    assert coverage_from_zip(zip_of(COVERAGE_PAYLOAD, "out/coverage.json")) != {}
    # Not a zip at all.
    assert coverage_from_zip(b"definitely not a zip") == {}
    assert coverage_from_zip(b"") == {}


# --------------------------------------------------------------------------- #
# CI status
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state,expected", [
    ("success", "success"), ("failure", "failure"), ("error", "failure"),
    ("pending", "pending"), ("something-new", "unknown"),
])
def test_ci_status_maps_the_combined_status(state, expected):
    coll = CoverageCollector(FakeGithub(FakeRepo(FakeCommit(state=state))))
    assert coll.ci_status("acme/widgets", HEAD) == expected


def test_ci_status_falls_back_to_check_runs_when_there_are_no_statuses():
    runs = [NS(status="completed", conclusion="success"), NS(status="completed", conclusion="neutral")]
    commit = FakeCommit(state="pending", total=0, check_runs=runs)
    assert CoverageCollector(FakeGithub(FakeRepo(commit))).ci_status("r", HEAD) == "success"

    runs = [NS(status="completed", conclusion="success"), NS(status="completed", conclusion="failure")]
    commit = FakeCommit(state="pending", total=0, check_runs=runs)
    assert CoverageCollector(FakeGithub(FakeRepo(commit))).ci_status("r", HEAD) == "failure"

    runs = [NS(status="in_progress", conclusion=None)]
    commit = FakeCommit(state="pending", total=0, check_runs=runs)
    assert CoverageCollector(FakeGithub(FakeRepo(commit))).ci_status("r", HEAD) == "pending"


def test_ci_status_is_unknown_when_nothing_can_be_read():
    assert CoverageCollector(None).ci_status("r", HEAD) == "unknown"
    assert CoverageCollector(FakeGithub(FakeRepo(None))).ci_status("r", HEAD) == "unknown"
    commit = FakeCommit(raise_status=True, check_runs=[])
    assert CoverageCollector(FakeGithub(FakeRepo(commit))).ci_status("r", HEAD) == "unknown"
    assert CoverageCollector(FakeGithub()).ci_status("r", "") == "unknown"


# --------------------------------------------------------------------------- #
# collect()
# --------------------------------------------------------------------------- #


def test_posted_coverage_wins_and_is_real():
    coll = CoverageCollector(FakeGithub())
    frag = coll.collect("acme/widgets", HEAD, posted=COVERAGE_PAYLOAD)
    assert frag.present is True
    assert frag.source == "posted"
    assert frag.provenance == "real"
    assert frag.ci_status == "success"
    assert frag.head_sha == HEAD
    assert frag.files["provisioning/sync.py"].missing == [4, 5]


def test_artifact_coverage_is_downloaded_with_the_token():
    http = FakeHttp(content=zip_of(COVERAGE_PAYLOAD))
    repo = FakeRepo(runs=[run_with_artifact()])
    coll = CoverageCollector(FakeGithub(repo), token="tok", artifact_name="coverage-json", http_client=http)
    frag = coll.collect("acme/widgets", HEAD)
    assert frag.source == "github-artifact"
    assert frag.provenance == "real"
    assert frag.present is True
    url, headers, follow = http.calls[0]
    assert url == "https://api/artifacts/1/zip"
    assert headers == {"Authorization": "Bearer tok"}
    assert follow is True


def test_an_artifact_with_the_wrong_name_is_ignored():
    http = FakeHttp(content=zip_of(COVERAGE_PAYLOAD))
    repo = FakeRepo(runs=[run_with_artifact(name="some-other-artifact")])
    frag = CoverageCollector(FakeGithub(repo), http_client=http).collect("acme/widgets", HEAD)
    assert frag.present is False and frag.source == "none"
    assert http.calls == []


def test_a_failing_download_is_a_missing_section_not_an_exception():
    repo = FakeRepo(runs=[run_with_artifact()])
    frag = CoverageCollector(FakeGithub(repo), http_client=FakeHttp(explode=True)).collect("acme/widgets", HEAD)
    assert frag.present is False and frag.source == "none"
    frag = CoverageCollector(FakeGithub(repo), http_client=FakeHttp(status_code=404)).collect("acme/widgets", HEAD)
    assert frag.present is False


def test_nothing_real_and_no_simulate_flag_gives_present_false():
    frag = CoverageCollector(FakeGithub()).collect(
        "acme/widgets", HEAD, added_lines={"a.py": [1, 2, 3]}, simulate=False)
    assert frag.present is False
    assert frag.source == "none"
    assert frag.provenance == "real"
    assert frag.files == {}
    assert frag.ci_status == "success"          # status is still collected


def test_the_simulated_fallback_is_labelled_simulated():
    added = {"provisioning/sync.py": [1, 2, 3, 4, 5]}
    frag = CoverageCollector(FakeGithub()).collect(
        "acme/widgets", HEAD, added_lines=added, simulate=True)
    assert frag.present is True
    assert frag.source == "simulated"
    assert frag.provenance == "simulated"       # RFC section 0, rule 7
    assert frag.files == simulate_coverage(added)


def test_simulate_with_no_added_lines_still_gives_present_false():
    frag = CoverageCollector(FakeGithub()).collect("acme/widgets", HEAD, simulate=True)
    assert frag.present is False and frag.source == "none"


def test_real_coverage_beats_the_simulator():
    http = FakeHttp(content=zip_of(COVERAGE_PAYLOAD))
    repo = FakeRepo(runs=[run_with_artifact()])
    frag = CoverageCollector(FakeGithub(repo), http_client=http).collect(
        "acme/widgets", HEAD, added_lines={"a.py": [1]}, simulate=True)
    assert frag.source == "github-artifact" and frag.provenance == "real"


def test_collect_with_no_clients_at_all_never_raises():
    frag = CoverageCollector().collect("acme/widgets", HEAD)
    assert frag.present is False
    assert frag.ci_status == "unknown"
    assert frag.model_validate(json.loads(frag.model_dump_json())) == frag

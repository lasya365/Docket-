"""`make doctor`: check every configured integration and say what is wrong, in plain words.

    python -m docket.doctor              # everything
    python -m docket.doctor --offline    # local checks only, no network

It only reads. It never posts a commit status, never creates a Freshservice change,
never generates a token of Claude output (Anthropic is checked through the Models
API, which is free), and never prints a secret.

Assumptions written down as RFC section 0 rule 2 asks:
  * Freshservice endpoints differ between accounts (RFC section 20). Every URL the
    doctor calls is in FRESHSERVICE_ENDPOINTS below, so a wrong one is fixed in one
    place. An endpoint the doctor cannot read is reported as a warning, not a
    failure: the first real run is the final word, and its error lands in
    `outputs.errors` naming the field.
  * GitHub commit-status WRITE access cannot be proven without writing a status,
    so it is reported as unverified rather than guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from docket.settings import Settings, load_settings

OK, WARN, FAIL, SKIP, INFO = "ok", "warn", "fail", "skip", "info"

# The only Freshservice URLs the doctor reads. Change them here if your account differs.
FRESHSERVICE_ENDPOINTS = {
    "auth": "/changes?per_page=1",
    "requester": "/requesters/{id}",
    "agent": "/agents/{id}",
    "change_fields": "/change_form_fields",
}

PLACEHOLDER_REPO = "owner/name"
PLACEHOLDER_DOMAIN = "yourcompany.freshservice.com"
PUBLIC_PLACEHOLDERS = {"change-me"}


@dataclass
class Check:
    section: str
    status: str
    title: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    def add(self, section: str, status: str, title: str, fix: str = "") -> None:
        self.checks.append(Check(section, status, self._scrub(title), self._scrub(fix)))

    def _scrub(self, text: str) -> str:
        """Belt and braces: no secret value ever reaches the screen, even inside an error.

        The shipped example value is public (it is in .env.example), and hiding it
        would hide the very warning that says "you are still using the example".
        """
        for secret in self.secrets:
            if secret and len(secret) >= 6 and secret not in PUBLIC_PLACEHOLDERS:
                text = text.replace(secret, "[redacted]")
        return text

    def count(self, status: str) -> int:
        return sum(1 for c in self.checks if c.status == status)

    @property
    def failed(self) -> bool:
        return self.count(FAIL) > 0


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:200]


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def check_environment(settings: Settings, report: Report) -> None:
    s = "Environment"
    v = sys.version_info
    if v >= (3, 11):
        report.add(s, OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        report.add(s, FAIL, f"Python {v.major}.{v.minor} is too old", "install Python 3.11 or newer")

    report.add(s, OK, f"config loaded from {settings.config_path}")
    for problem in settings.check_invariants():
        report.add(s, WARN, problem, "revert the gate weights or threshold in config.json")

    token = settings.ingest_token
    if not token:
        report.add(s, WARN, "DOCKET_INGEST_TOKEN is empty: anyone who can reach the port can post fake sessions",
                   "run `make env`, or set DOCKET_INGEST_TOKEN in .env")
    elif token == "change-me":
        report.add(s, WARN, "DOCKET_INGEST_TOKEN is still the example value `change-me`",
                   "run `make env` for a random one, or set your own in .env")
    else:
        report.add(s, OK, "DOCKET_INGEST_TOKEN is set")

    if settings.api_token:
        report.add(s, OK, "DOCKET_API_TOKEN is set (paste it into the UI's API token field)")
    else:
        report.add(s, WARN, "DOCKET_API_TOKEN is empty: run, seed, reset and the threshold are open",
                   "fine on a laptop; set DOCKET_API_TOKEN in .env before anyone else can reach the port")

    # Read-only: the doctor must not create the database it is checking.
    db = settings.path(settings.db_path)
    if db.exists():
        try:
            sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("SELECT 1").fetchone()
            writable = os.access(db, os.W_OK)
            report.add(s, OK if writable else FAIL, f"database {'is writable' if writable else 'is read-only'}: {db}",
                       "" if writable else "fix the file's permissions")
        except Exception as exc:
            report.add(s, FAIL, f"{db} exists but is not a readable SQLite database: {_short(exc)}",
                       "move it aside; Docket creates a fresh one on start")
    else:
        parent = next((p for p in [db.parent, *db.parent.parents] if p.exists()), db.parent)
        if os.access(parent, os.W_OK):
            report.add(s, OK, f"database will be created on first start: {db}")
        else:
            report.add(s, FAIL, f"cannot create the database: {parent} is not writable", "point DOCKET_DB somewhere writable")

    if (settings.repo_root / "frontend" / "index.html").exists():
        report.add(s, OK, "frontend/index.html is present")
    else:
        report.add(s, FAIL, "frontend/index.html is missing", "restore it from the release")

    if settings.mode == "replay":
        report.add(s, INFO, "DOCKET_MODE=replay: POST /run/{pr} uses demo/seed before GitHub")
    elif settings.mode != "live":
        report.add(s, FAIL, f"DOCKET_MODE={settings.mode!r} is not a mode", "use live or replay")


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github(token: str) -> Any:
    from github import Auth, Github

    return Github(auth=Auth.Token(token), timeout=10, retry=None, per_page=1)


def check_github(settings: Settings, report: Report, factory: Callable[[str], Any] = _github) -> None:
    s = "GitHub"
    token = settings.github_token
    if not token:
        report.add(s, SKIP, "GITHUB_TOKEN not set: only the seeded demo and replay mode work",
                   "see README section 3.2 to connect a repository")
        return

    from github import BadCredentialsException, GithubException, UnknownObjectException

    gh = factory(token)
    try:
        login = gh.get_user().login
        report.add(s, OK, f"token is valid (acts as {login})")
    except BadCredentialsException:
        report.add(s, FAIL, "GitHub rejected the token (401 bad credentials)",
                   "create a new token and put it in GITHUB_TOKEN; it may be expired or revoked")
        return
    except GithubException as exc:
        # A fine-grained or app token may not be allowed to read /user; that is fine.
        if getattr(exc, "status", None) not in (403, 404):
            report.add(s, FAIL, f"could not reach GitHub: {_short(exc)}", "check the network and the token")
            return
    except Exception as exc:
        report.add(s, FAIL, f"could not reach GitHub: {_short(exc)}", "check the network and any proxy")
        return

    scopes = getattr(gh, "oauth_scopes", None)
    if scopes:                              # only classic tokens report scopes
        if "repo" not in scopes and "repo:status" not in scopes:
            report.add(s, FAIL, f"classic token lacks the `repo` scope (has: {', '.join(scopes)})",
                       "regenerate it with the `repo` scope, or use a fine-grained token")
        else:
            report.add(s, OK, f"classic token scopes include {'repo' if 'repo' in scopes else 'repo:status'}")

    repo_name = settings.config.github.default_repo
    if not repo_name or repo_name == PLACEHOLDER_REPO:
        report.add(s, FAIL, f"github.default_repo is still `{PLACEHOLDER_REPO}` in config.json",
                   "set it to the governed repository, e.g. `acme/provisioning-service`")
        return
    try:
        repo = gh.get_repo(repo_name)
        report.add(s, OK, f"repository {repo_name} is readable")
    except UnknownObjectException:
        report.add(s, FAIL, f"repository {repo_name} was not found, or this token cannot see it",
                   "check the spelling in github.default_repo, and that the token has access to that repo")
        return
    except GithubException as exc:
        report.add(s, FAIL, f"cannot open {repo_name}: {_short(exc)}", "check the token's repository access")
        return

    probes = [
        ("Contents: read", "commits and changed files", lambda: next(iter(repo.get_commits()), None)),
        ("Pull requests: read", "the PR, its reviews and comments", lambda: next(iter(repo.get_pulls(state="all")), None)),
        ("Issues: read", "the linked issue, its Scope: line, review requests", lambda: next(iter(repo.get_issues(state="all")), None)),
        ("Actions: read", "CI coverage artifacts", lambda: next(iter(repo.get_workflow_runs()), None)),
    ]
    for perm, why, probe in probes:
        try:
            probe()
            report.add(s, OK, f"{perm} ({why})")
        except GithubException as exc:
            status = getattr(exc, "status", "?")
            report.add(s, FAIL, f"missing {perm}: GitHub answered {status} when reading {why}",
                       f"give the token `{perm}` on {repo_name}")
        except Exception as exc:
            report.add(s, WARN, f"could not check {perm}: {_short(exc)}")

    try:
        branch = repo.get_branch(repo.default_branch)
        repo.get_commit(branch.commit.sha).get_combined_status()
        report.add(s, INFO, "Commit statuses: read works. WRITE cannot be tested without posting a status; "
                            "the first real run proves it (a failure lands in the run's outputs.errors)",
                   "the token needs `Commit statuses: read and write`")
    except GithubException as exc:
        report.add(s, FAIL, f"cannot read commit statuses ({getattr(exc, 'status', '?')})",
                   "give the token `Commit statuses: read and write`")
    except Exception as exc:
        report.add(s, WARN, f"could not check commit statuses: {_short(exc)}")

    name = settings.config.github.coverage_artifact_name
    try:
        seen = False
        for i, run in enumerate(repo.get_workflow_runs()):
            if i >= 10:
                break
            if any(a.name == name for a in run.get_artifacts()):
                seen = True
                break
        if seen:
            report.add(s, OK, f"a recent CI run uploaded the `{name}` coverage artifact")
        else:
            report.add(s, WARN, f"no recent CI run uploaded an artifact named `{name}`",
                       "add the coverage job from README 3.2; until then Untested generation is not computable")
    except Exception as exc:
        report.add(s, WARN, f"could not look for coverage artifacts: {_short(exc)}")

    try:
        limits = gh.get_rate_limit()
        core = getattr(getattr(limits, "resources", None), "core", None) or limits.core
        level = OK if core.remaining > 100 else WARN
        report.add(s, level, f"API rate limit: {core.remaining} of {core.limit} requests left")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Freshservice
# ---------------------------------------------------------------------------

def _freshservice(settings: Settings) -> Any:
    import httpx

    return httpx.Client(
        base_url=f"https://{settings.config.freshservice.domain}/api/v2",
        auth=(settings.freshservice_api_key, "X"),
        timeout=10,
    )


def _field_names(body: Any) -> set[str]:
    """Every `name` of every field-like object, wherever the account nests them."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and ("label" in node or "field_type" in node or "choices" in node):
                names.add(node["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return names


def check_freshservice(settings: Settings, report: Report, client_factory: Callable[[Settings], Any] = _freshservice) -> None:
    s = "Freshservice"
    fs = settings.config.freshservice
    key = settings.freshservice_api_key

    if not fs.enabled and not key:
        report.add(s, SKIP, "disabled: changes are written as JSON to out/freshservice/ (dry run)",
                   "see README section 3.3 to connect Freshservice")
        return
    if fs.enabled and not key:
        report.add(s, FAIL, "freshservice.enabled is true but FRESHSERVICE_API_KEY is empty",
                   "put the key in .env, or set enabled to false for dry run")
        return
    if not fs.enabled:
        report.add(s, WARN, "FRESHSERVICE_API_KEY is set but freshservice.enabled is false, so nothing is filed",
                   "set freshservice.enabled to true in config.json when you are ready")
    if not fs.domain or fs.domain == PLACEHOLDER_DOMAIN:
        report.add(s, FAIL, f"freshservice.domain is still `{PLACEHOLDER_DOMAIN}`",
                   "set it to your account, e.g. `acme.freshservice.com`")
        return

    client = client_factory(settings)
    try:
        r = client.get(FRESHSERVICE_ENDPOINTS["auth"])
    except Exception as exc:
        report.add(s, FAIL, f"cannot reach {fs.domain}: {_short(exc)}", "check the domain and the network")
        return
    if r.status_code == 401:
        report.add(s, FAIL, "Freshservice rejected the API key (401)",
                   "copy the key again from Profile settings -> Your API key")
        return
    if r.status_code == 403:
        report.add(s, FAIL, "the API key's user cannot read changes (403)",
                   "use the key of an agent with access to the Change module")
        return
    if r.status_code == 404:
        # Seen live: Freshservice answers 404 for a subdomain with no account behind it.
        report.add(s, FAIL, f"no Freshservice account answers at {fs.domain} (HTTP 404)",
                   "check freshservice.domain for a typo; it is the host you log in to, e.g. acme.freshservice.com")
        return
    if r.status_code >= 400:
        report.add(s, FAIL, f"reading changes failed with HTTP {r.status_code}", "check the domain and the account")
        return
    report.add(s, OK, f"API key works against {fs.domain}")

    if not fs.requester_id:
        report.add(s, FAIL, "freshservice.requester_id is 0",
                   "set it to the id of a real requester or agent in your account")
    else:
        found = False
        for kind in ("requester", "agent"):
            try:
                if client.get(FRESHSERVICE_ENDPOINTS[kind].format(id=fs.requester_id)).status_code == 200:
                    report.add(s, OK, f"requester_id {fs.requester_id} exists (an {kind})")
                    found = True
                    break
            except Exception:
                pass
        if not found:
            report.add(s, FAIL, f"requester_id {fs.requester_id} is neither a requester nor an agent in this account",
                       "look the id up in Freshservice and put it in config.json")

    wanted = fs.custom_fields.model_dump()
    try:
        r = client.get(FRESHSERVICE_ENDPOINTS["change_fields"])
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        names = _field_names(r.json())
    except Exception as exc:
        report.add(s, WARN, f"could not list the Change form fields ({_short(exc)}); the custom field names "
                            f"will be checked on the first real run",
                   "if that run fails, its error names the field")
        return
    skipped = [role for role, name in wanted.items() if not name]     # "" = not sent by the writer
    missing = {role: name for role, name in wanted.items() if name and name not in names}
    if skipped:
        report.add(s, INFO, f"custom field(s) not sent: {', '.join(skipped)} (name is empty in config.json)",
                   "create them on the Change form and set their names to file those values as fields")
    elif not missing:
        report.add(s, OK, "all four Docket custom fields exist on the Change form")
    for role, name in missing.items():
        similar = sorted(n for n in names if "docket" in n.lower() or role in n.lower())
        hint = f"did you mean {', '.join(similar[:3])}?" if similar else "create it (README section 3.3, step 1)"
        report.add(s, FAIL, f"custom field `{name}` (for the {role}) is not on the Change form",
                   f"{hint} then set freshservice.custom_fields.{role} in config.json")
    report.add(s, INFO, "the Workflow Automator rule that requests CAB approval cannot be checked through the API",
               "make sure it exists: when Docket decision is HOLD, request approval from the CAB group")


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _anthropic(key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=10)


def check_anthropic(settings: Settings, report: Report, factory: Callable[[str], Any] = _anthropic) -> None:
    s = "Anthropic (board brief)"
    brief = settings.config.brief
    if not brief.enabled:
        report.add(s, SKIP, "brief.enabled is false: no brief is written")
        return
    if not settings.anthropic_api_key:
        report.add(s, SKIP, "ANTHROPIC_API_KEY not set: the brief is a template. The decision is identical either way",
                   "optional: add the key to .env for a written brief")
        return

    import anthropic

    try:
        info = factory(settings.anthropic_api_key).models.retrieve(brief.model)
        report.add(s, OK, f"key works and model `{getattr(info, 'id', brief.model)}` is available")
    except anthropic.AuthenticationError:
        report.add(s, FAIL, "Anthropic rejected the API key (401)",
                   "create a key at console.anthropic.com and put it in ANTHROPIC_API_KEY")
    except anthropic.PermissionDeniedError:
        report.add(s, FAIL, "the key is valid but not allowed to use the Models API (403)",
                   "check the key's workspace permissions")
    except anthropic.NotFoundError:
        report.add(s, FAIL, f"model `{brief.model}` is not available to this key",
                   "set brief.model in config.json to a model your account can use")
    except anthropic.RateLimitError:
        report.add(s, WARN, "rate limited (429): the key works, the account is busy")
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        report.add(s, FAIL, f"cannot reach the Anthropic API: {_short(exc)}", "check the network and any proxy")
    except anthropic.APIStatusError as exc:
        report.add(s, FAIL, f"Anthropic answered HTTP {exc.status_code}", _short(exc))



# ---------------------------------------------------------------------------
# telemetry: is the agent actually reaching Docket?
# ---------------------------------------------------------------------------

def check_telemetry(settings: Settings, report: Report, store: Any | None = None) -> None:
    """Read the store directly: has any agent session arrived, and is it complete?

    A session that arrives without prompts, tool details or edit text scores very
    differently from one that arrives whole, and the cause is always a missing
    environment variable or a hook that is not firing. Saying so here costs one
    query and saves an agent run.
    """
    s = "Telemetry from the agent"
    if store is None:
        try:
            from docket.record.store import Store

            db = settings.path(settings.db_path)
            if not db.exists():
                report.add(s, INFO, "no database yet: nothing has been ingested",
                           "start Docket and run one agent session in a governed repository")
                return
            store = Store(db)
        except Exception as exc:
            report.add(s, WARN, f"could not open the store: {_short(exc)}")
            return

    try:
        sessions = store.list_sessions()
    except Exception as exc:
        report.add(s, WARN, f"could not read sessions: {_short(exc)}")
        return

    if not sessions:
        report.add(s, WARN, "no agent session has ever reached this Docket",
                   "in the governed repo: source the telemetry environment BEFORE starting the agent, "
                   "and check the ingest token matches .env")
        return

    report.add(s, OK, f"{len(sessions)} agent session(s) received")

    newest = max(sessions, key=lambda r: str(r.get("ended_at") or ""))
    without_edits = [r for r in sessions if not r.get("edits")]
    if without_edits:
        report.add(
            s, WARN,
            f"{len(without_edits)} of {len(sessions)} session(s) carry no edit text",
            "the PostToolUse hook is not reaching Docket. Its `url` in .claude/settings.json must be a "
            "literal address - environment variables are NOT expanded there. Without edits every added "
            "line is labelled `unknown` instead of `ai`.",
        )
    else:
        report.add(s, OK, "every session carries edit text")

    try:
        from docket.collectors.adapters.claude_code import build_session

        events, edits, meta = store.load_session_parts(newest["session_id"])
        fragment = build_session(newest["session_id"], events, edits, meta)
    except Exception as exc:
        report.add(s, WARN, f"could not assemble the newest session: {_short(exc)}")
        return
    if fragment is None:
        report.add(s, WARN, "the newest session has no usable events")
        return

    detail = fragment.detail
    if not detail.prompts:
        report.add(s, WARN, "the newest session carries no prompt text",
                   "set OTEL_LOG_USER_PROMPTS=1 before starting the agent; Claude Code redacts prompts by default")
    if not detail.tool_details:
        report.add(s, WARN, "the newest session carries no tool details",
                   "set OTEL_LOG_TOOL_DETAILS=1; without it MCP tools report only as `mcp_tool` and the "
                   "blast-radius signal runs degraded")
    if detail.prompts and detail.tool_details and detail.edits:
        report.add(s, OK, "the newest session is complete: prompts, tool details and edits all present")
    if not fragment.repo_url:
        report.add(s, INFO, "the newest session reports no repository URL",
                   "optional: OTEL_METRICS_INCLUDE_REPOSITORY=true, and the repo needs an `origin` remote")


# ---------------------------------------------------------------------------
# Claude Code and the running server
# ---------------------------------------------------------------------------

def _http_get(url: str) -> tuple[int, Any]:
    import httpx

    r = httpx.get(url, timeout=2)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def check_agent_setup(settings: Settings, report: Report, http_get: Callable[[str], tuple[int, Any]] = _http_get,
                      which: Callable[[str], Any] = shutil.which, offline: bool = False) -> None:
    s = "Claude Code and the server"
    base = settings.config.server.public_base_url.rstrip("/")
    setup = settings.repo_root / "agent-setup"

    env_file = setup / "docket-env.sh"
    if env_file.exists():
        m = re.search(r"^export DOCKET_INGEST_TOKEN=(\S+)", env_file.read_text(), re.M)
        template = m.group(1) if m else ""
        if settings.ingest_token and template != settings.ingest_token:
            report.add(s, INFO, "agent-setup/docket-env.sh does not carry your ingest token yet",
                       "set DOCKET_INGEST_TOKEN in the copy of docket-env.sh you install in the governed repo "
                       "to the same value as Docket's .env")
        m = re.search(r"^export DOCKET_URL=(\S+)", env_file.read_text(), re.M)
        if m and urlparse(m.group(1)).netloc != urlparse(base).netloc:
            report.add(s, WARN, f"docket-env.sh points at {m.group(1)} but server.public_base_url is {base}",
                       "make them agree")

    settings_json = setup / ".claude" / "settings.json"
    if settings_json.exists():
        try:
            hooks = json.loads(settings_json.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
            if urlparse(hooks.get("url", "")).netloc != urlparse(base).netloc:
                report.add(s, WARN, f"the edit hook posts to {hooks.get('url')}, not to {base}",
                           "change the url in .claude/settings.json in the governed repo")
            else:
                report.add(s, OK, "the edit hook points at this Docket")
        except Exception as exc:
            report.add(s, WARN, f"could not read agent-setup/.claude/settings.json: {_short(exc)}")

    missing = [tool for tool in ("jq", "curl", "git") if not which(tool)]
    if missing:
        report.add(s, WARN, f"{', '.join(missing)} not found on this machine",
                   "the SessionStart hook needs jq, curl and git on each developer's machine")
    else:
        report.add(s, OK, "jq, curl and git are installed (the hooks need them on developer machines)")

    if offline:
        report.add(s, SKIP, "--offline: did not contact the running server")
        return
    try:
        status, body = http_get(f"{base}/healthz")
    except Exception:
        report.add(s, INFO, f"Docket is not running at {base}", "start it with `make serve` to check it live")
        return
    if status != 200:
        # Seen live: another service already owned the port, so /healthz 404s.
        report.add(
            s, FAIL,
            f"something is listening at {base} but it is not Docket (HTTP {status} on /healthz)",
            f"either Docket runs on another port - start it with `make serve PORT=...` and set "
            f"server.public_base_url in config.json to match - or another service owns this one",
        )
        return
    report.add(s, OK, f"Docket is running at {base}")
    if isinstance(body, dict) and body.get("ingest_authenticated") is False:
        report.add(s, WARN, "the running server has no ingest token",
                   "it was started without .env; stop it and use `make serve`")
    try:
        _status, sessions = http_get(f"{base}/sessions")
        n = len(sessions) if isinstance(sessions, list) else 0
        if n:
            report.add(s, OK, f"{n} agent session(s) received")
        else:
            report.add(s, INFO, "no agent sessions received yet",
                       "install agent-setup/ in a governed repo, `source docket-env.sh`, then start claude")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# running and printing
# ---------------------------------------------------------------------------

def run(settings: Settings, *, offline: bool = False, github_factory=_github, freshservice_factory=_freshservice,
        anthropic_factory=_anthropic, http_get=_http_get, which=shutil.which) -> Report:
    report = Report(secrets=[settings.github_token, settings.freshservice_api_key, settings.anthropic_api_key,
                             settings.ingest_token, settings.api_token])
    check_environment(settings, report)
    if offline:
        for section in ("GitHub", "Freshservice", "Anthropic (board brief)"):
            report.add(section, SKIP, "--offline: not contacted")
    else:
        for fn, factory in ((check_github, github_factory), (check_freshservice, freshservice_factory),
                            (check_anthropic, anthropic_factory)):
            try:
                fn(settings, report, factory)
            except Exception as exc:                     # one broken check never hides the others
                report.add(fn.__name__.replace("check_", "").title(), FAIL, f"the check itself crashed: {_short(exc)}")
    try:
        check_telemetry(settings, report)
    except Exception as exc:
        report.add("Telemetry from the agent", WARN, f"check failed: {_short(exc)}")
    check_agent_setup(settings, report, http_get=http_get, which=which, offline=offline)
    return report


MARKS = {OK: ("✓", "32"), WARN: ("!", "33"), FAIL: ("✗", "31"), SKIP: ("–", "90"), INFO: ("i", "36")}


def render(report: Report, color: bool = False) -> str:
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = ["Docket doctor", ""]
    section = None
    for c in report.checks:
        if c.section != section:
            section = c.section
            lines.append(paint(section, "1"))
        mark, code = MARKS[c.status]
        lines.append(f"  {paint(mark, code)} {c.title}")
        if c.fix and c.status in (FAIL, WARN, INFO, SKIP):
            lines.append(f"      {paint('→', '90')} {c.fix}")
    lines.append("")
    summary = f"{report.count(OK)} ok, {report.count(WARN)} warnings, {report.count(FAIL)} failures"
    lines.append(paint(summary, "31" if report.failed else ("33" if report.count(WARN) else "32")))
    if report.failed:
        lines.append("Fix the ✗ lines first; each one says how.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check every configured Docket integration.")
    parser.add_argument("--offline", action="store_true", help="local checks only; contact nothing")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"✗ config.json could not be loaded: {_short(exc)}")
        return 1
    report = run(settings, offline=args.offline)
    print(render(report, color=sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

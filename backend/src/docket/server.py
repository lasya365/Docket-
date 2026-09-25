"""FastAPI app and routes (RFC section 11).

Rules that shape this file:
  * Ingest routes (/v1/*, /hooks/*) are authenticated with the ingest token and
    compared with hmac.compare_digest (RFC 17.1). When DOCKET_INGEST_TOKEN is
    empty the routes stay open and a warning is logged once at startup, so a
    first run works out of the box; set the token before anyone else can reach
    the port.
  * No CORS headers: same origin only (RFC 17.6).
  * Never 5xx on a malformed ingest record. Skip it, log it, keep the rest.
  * /hooks/claude-code always answers 200 with an EMPTY body. A JSON body would
    be parsed by Claude Code as a hook decision (RFC 7.2).
  * PUT /config/threshold re-gates the stored runs. Assumption (RFC 0 rule 2):
    a re-gated run is re-sealed, so GET /changes/{key}/verify keeps telling the
    truth about the run as stored; Freshservice is deliberately not rewritten,
    so the filed ticket keeps the seal of the decision that was filed.
"""

from __future__ import annotations

import gzip
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from docket import pipeline
from docket.collectors import local_git, otlp
from docket.collectors.hooks_ingest import parse_hook_payload
from docket.collectors.watchers import manifest as manifest_watcher
from docket.decide.gate import regate
from docket.record.seal import seal as seal_run
from docket.settings import Settings, get_settings

log = logging.getLogger("docket.server")

OTLP_OK = {"partialSuccess": {}}
WRONG_PROTOCOL = "Set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else authorization.strip()


def _token_ok(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def require_ingest(settings: Settings, authorization: str | None) -> None:
    expected = settings.ingest_token
    if not expected:
        return                                    # dev mode; warned about at startup
    if not _token_ok(_bearer(authorization), expected):
        raise HTTPException(status_code=401, detail="bad ingest token")


def require_api(settings: Settings, authorization: str | None) -> None:
    expected = settings.api_token
    if not expected:
        return                                    # optional in dev (RFC section 11)
    if not _token_ok(_bearer(authorization), expected):
        raise HTTPException(status_code=401, detail="bad api token")


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def create_app(
    settings: Settings | None = None,
    store: Any = None,
    deps: pipeline.Deps | None = None,
    background_watcher: bool = False,
) -> FastAPI:
    settings = settings or get_settings()
    frontend = settings.repo_root / "frontend" / "index.html"

    if store is None and deps is None:
        from docket.record.store import Store

        store = Store(settings.path(settings.db_path))
    if deps is None:
        deps = _build_deps(settings, store)
    store = deps.store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.ingest_token:
            log.warning(
                "DOCKET_INGEST_TOKEN is empty: /v1/* and /hooks/* are open on this host. "
                "Set it before exposing the port (RFC 17.1)."
            )
        for problem in settings.check_invariants():
            log.warning(problem)
        task = None
        if background_watcher and settings.config.manifest.poll_seconds > 0:
            import asyncio

            async def poll() -> None:
                while True:
                    await asyncio.sleep(settings.config.manifest.poll_seconds)
                    try:
                        _manifest_check(settings, deps)
                    except Exception as exc:                  # a watcher never kills the server
                        log.warning("manifest poll failed: %s", exc)

            task = asyncio.create_task(poll())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="Docket", version="3.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.deps = deps
    app.state.store = store

    # -- plane 1 ingest ----------------------------------------------------

    async def _otlp_body(request: Request) -> Any:
        content_type = (request.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            return Response(status_code=415, content=WRONG_PROTOCOL, media_type="text/plain")
        raw = await request.body()
        if (request.headers.get("content-encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception as exc:
                log.debug("gzip body could not be decompressed: %s", exc)
                return None
        try:
            return json.loads(raw or b"{}")
        except Exception as exc:
            log.debug("malformed OTLP body: %s", exc)
            return None

    @app.post("/v1/logs")
    async def otlp_logs(request: Request, authorization: str | None = Header(default=None)):
        require_ingest(settings, authorization)
        payload = await _otlp_body(request)
        if isinstance(payload, Response):
            return payload
        if payload is None:
            return JSONResponse(OTLP_OK)
        try:
            events = otlp.flatten(payload)
        except Exception as exc:                              # never 5xx on a bad record
            log.debug("could not flatten OTLP payload: %s", exc)
            return JSONResponse(OTLP_OK)
        stored = 0
        for event in events:
            try:
                store.save_event(
                    str(event.attrs.get("session.id") or "_none"),
                    event.name,
                    event.timestamp,
                    event.attrs,
                )
                stored += 1
            except Exception as exc:
                log.debug("dropping one event: %s", exc)
        log.debug("stored %d/%d OTLP log records", stored, len(events))
        return JSONResponse(OTLP_OK)

    @app.post("/v1/metrics")
    async def otlp_metrics(request: Request, authorization: str | None = Header(default=None)):
        # The route must exist or the exporter logs errors. v3 does not use metrics.
        require_ingest(settings, authorization)
        payload = await _otlp_body(request)
        return payload if isinstance(payload, Response) else JSONResponse(OTLP_OK)

    @app.post("/v1/traces")
    async def otlp_traces(request: Request, authorization: str | None = Header(default=None)):
        require_ingest(settings, authorization)
        payload = await _otlp_body(request)
        return payload if isinstance(payload, Response) else JSONResponse(OTLP_OK)

    @app.post("/hooks/claude-code")
    async def hook_ingest(request: Request, authorization: str | None = Header(default=None)):
        require_ingest(settings, authorization)
        empty = Response(status_code=200, content=b"")        # never a JSON body (RFC 7.2)
        try:
            payload = json.loads(await request.body() or b"{}")
        except Exception as exc:
            log.debug("malformed hook payload: %s", exc)
            return empty
        try:
            result = parse_hook_payload(payload)
        except Exception as exc:
            log.debug("could not parse hook payload: %s", exc)
            return empty
        try:
            if result.session_id:
                store.upsert_session_meta(result.session_id, **_hook_meta(result))
            if result.edit is not None:
                store.save_edit_event(result.edit)
        except Exception as exc:
            log.warning("could not store hook payload: %s", exc)
        return empty

    @app.post("/coverage/{head_sha}")
    async def post_coverage(head_sha: str, payload: dict = Body(...), authorization: str | None = Header(default=None)):
        require_ingest(settings, authorization)
        store.save_coverage(head_sha, payload)
        return {"stored": True}

    # -- runs --------------------------------------------------------------

    # Declared before /run/{pr}: that route takes an int, so "local" would 422 there.
    @app.post("/run/local")
    async def start_local_run(
        background: BackgroundTasks,
        payload: dict = Body(...),
        authorization: str | None = Header(default=None),
    ):
        """Score a local branch that has no pull request (RFC 7.3, local flavour)."""
        require_api(settings, authorization)
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail='body must be {"path": "/abs/path/to/repo"}')
        base = str(payload.get("base") or "main").strip() or "main"
        head = str(payload.get("head") or "HEAD").strip() or "HEAD"
        try:
            repo, head_sha = local_git.repo_identity(path, head)
        except Exception as exc:
            # Resolved here, not in the background task, because the caller is owed
            # the change_key in this response.
            raise HTTPException(status_code=400, detail=str(exc))
        run_id = pipeline.new_run_id()
        change_key = pipeline.change_key_for_local(repo, head_sha)
        store.set_status(run_id, "collecting", f"queued {repo} {base}..{head}")
        background.add_task(_run_local, deps, path, base, head, run_id)
        return {"run_id": run_id, "change_key": change_key}

    @app.post("/run/{pr}")
    async def start_run(
        pr: int,
        background: BackgroundTasks,
        repo: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        require_api(settings, authorization)
        repo = repo or settings.config.github.default_repo
        run_id = pipeline.new_run_id()
        change_key = pipeline.change_key_for_pr(repo, pr)
        store.set_status(run_id, "collecting", f"queued {repo}#{pr}")
        background.add_task(_run_pr, settings, deps, repo, pr, run_id)
        return {"run_id": run_id, "change_key": change_key}

    @app.get("/status/{run_id}")
    async def run_status(run_id: str):
        status = store.get_status(run_id) or {"stage": "unknown", "message": "", "change_key": None}
        return status

    # -- reads -------------------------------------------------------------

    @app.get("/changes")
    async def list_changes():
        return store.list_changes()

    @app.get("/changes/{key}")
    async def get_change(key: str):
        run = store.latest_run(key)
        if run is None:
            raise HTTPException(status_code=404, detail="no such change")
        return json.loads(run.model_dump_json())

    @app.get("/changes/{key}/runs")
    async def get_runs(key: str):
        return store.runs_for(key)

    @app.get("/changes/{key}/verify")
    async def verify_change(key: str):
        run = store.latest_run(key)
        if run is None:
            raise HTTPException(status_code=404, detail="no such change")
        recomputed = seal_run(run)
        return {"match": hmac.compare_digest(recomputed, run.seal), "seal": run.seal}

    @app.post("/changes/{key}/sync")
    async def sync_change(key: str, authorization: str | None = Header(default=None)):
        require_api(settings, authorization)
        if deps.freshservice is None:
            raise HTTPException(status_code=503, detail="no freshservice writer configured")
        outputs = deps.freshservice.sync_approval(key, store)
        run = store.latest_run(key)
        if run is not None and deps.status is not None:
            try:
                deps.status.write(run)
                store.save_run(run)
            except Exception as exc:
                log.warning("status writer failed during sync: %s", exc)
        return json.loads(outputs.model_dump_json()) if hasattr(outputs, "model_dump_json") else outputs

    # -- config ------------------------------------------------------------

    @app.get("/config/gate")
    async def get_gate_config():
        gate = settings.gate_config(store.get_override("threshold"))
        body = json.loads(gate.model_dump_json())
        # The gate itself only needs the sensitive PATHS (HS3), but the UI highlights
        # sensitive tool calls and shell commands too, and RFC 0 rule 4 says it must
        # not hard-code them. The whole sensitive block rides along.
        body["sensitive"] = json.loads(settings.config.sensitive.model_dump_json())
        return body

    @app.put("/config/threshold")
    async def put_threshold(payload: dict = Body(...), authorization: str | None = Header(default=None)):
        require_api(settings, authorization)
        try:
            threshold = float(payload["threshold"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="body must be {\"threshold\": 0-100}")
        if not 0 <= threshold <= 100:
            raise HTTPException(status_code=400, detail="threshold must be between 0 and 100")
        store.set_override("threshold", threshold)
        gate = settings.gate_config(threshold)
        out = []
        for row in store.list_changes():
            run = store.latest_run(row["change_key"])
            if run is None:
                continue
            run.decision = regate(run, gate)
            run.seal = seal_run(run)
            store.save_run(run)
            out.append(
                {
                    "change_key": run.change_key,
                    "decision": run.decision.decision,
                    "composite": run.decision.composite,
                }
            )
        return out

    # -- manifest ----------------------------------------------------------

    @app.post("/manifest/check")
    async def manifest_check(authorization: str | None = Header(default=None)):
        require_api(settings, authorization)
        return _manifest_check(settings, deps)

    @app.get("/manifest/ledger")
    async def manifest_ledger(agent_id: str | None = Query(default=None)):
        return [json.loads(s.model_dump_json()) for s in store.ledger(agent_id)]

    # -- sessions ----------------------------------------------------------

    @app.get("/sessions")
    async def list_sessions():
        return store.list_sessions()

    # -- demo --------------------------------------------------------------

    @app.post("/demo/seed")
    async def demo_seed(authorization: str | None = Header(default=None)):
        require_api(settings, authorization)
        changes = []
        for seed in pipeline.seed_dirs(settings):
            try:
                run = pipeline.run_seed(seed, deps)
                changes.append(
                    {
                        "change_key": run.change_key,
                        "decision": run.decision.decision,
                        "composite": run.decision.composite,
                        "seed": seed.name,
                    }
                )
            except Exception as exc:
                log.warning("seed %s failed: %s", seed.name, exc)
                changes.append({"seed": seed.name, "error": str(exc)})
        return {"changes": changes}

    @app.post("/demo/reset")
    async def demo_reset(authorization: str | None = Header(default=None)):
        require_api(settings, authorization)
        store.reset_runs()
        return {"changes": []}

    # -- chrome ------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "mode": settings.mode,
            "ingest_authenticated": bool(settings.ingest_token),
            "time": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/")
    async def index():
        if frontend.exists():
            return FileResponse(frontend, media_type="text/html")
        raise HTTPException(status_code=404, detail="frontend/index.html is not built yet")

    return app


# ---------------------------------------------------------------------------
# helpers used by routes and by the background loop
# ---------------------------------------------------------------------------

def _hook_meta(result: Any) -> dict:
    """Session meta out of a parsed hook payload, as Store.upsert_session_meta wants it."""
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        meta = {f: getattr(result, f, None) for f in ("cwd", "repo_root", "permission_mode")}
    out = {}
    for src, dst in (("cwd", "cwd"), ("repo_root", "repo_root"), ("permission_mode", "mode")):
        value = meta.get(src)
        if value:
            out[dst] = value
    return out


def _run_pr(settings: Settings, deps: pipeline.Deps, repo: str, pr: int, run_id: str) -> None:
    try:
        if settings.mode == "replay":
            seed = _seed_for_pr(settings, pr)
            if seed is not None:
                pipeline.run_seed(seed, deps, run_id)
                return
        pipeline.run_code_change(repo, pr, deps, run_id)
    except Exception as exc:
        log.exception("run %s failed", run_id)
        try:
            deps.store.set_status(run_id, "error", str(exc))
        except Exception:
            pass


def _run_local(deps: pipeline.Deps, path: str, base: str, head: str, run_id: str) -> None:
    try:
        pipeline.run_local_change(path, base, head, deps, run_id)
    except Exception as exc:
        log.exception("local run %s failed", run_id)
        try:
            deps.store.set_status(run_id, "error", str(exc))
        except Exception:
            pass


def _seed_for_pr(settings: Settings, pr: int) -> Path | None:
    root = settings.repo_root / "demo" / "seed"
    named = root / f"pr-{pr}"
    if named.is_dir():
        return named
    for seed in pipeline.seed_dirs(settings):
        repo_file = seed / "repo_fragment.json"
        if not repo_file.exists():
            continue
        try:
            if json.loads(repo_file.read_text(encoding="utf-8")).get("pr_number") == pr:
                return seed
        except Exception:
            continue
    return None


def _manifest_check(settings: Settings, deps: pipeline.Deps) -> list[dict]:
    cfg = settings.config.manifest
    path = settings.path(cfg.path)
    if not path.exists():
        log.warning("manifest %s does not exist", path)
        return []
    manifest, _hash = manifest_watcher.load_manifest(path)
    latest = deps.store.latest_snapshot(manifest.agent_id)
    result = manifest_watcher.check(
        path,
        latest,
        now=deps.clock(),
        rehearsal_dir=settings.path(cfg.rehearsal_dir),
    )
    out: list[dict] = []
    if result.baseline is not None:
        # First sighting: store it so the next change has something to roll back to.
        deps.store.append_snapshot(result.baseline)
    for change in result.changes:
        run = pipeline.run_non_code_change(change, deps)
        out.append({"run_id": run.run_id, "change_key": run.change_key})
    return out


def _try(label: str, build):
    """Build one client. A client that cannot be built stays None and Docket degrades.

    Each client is wrapped separately on purpose: one shared try block once let a
    TypeError in the coverage collector's constructor silently disable the GitHub
    collector and the status writer alongside it.
    """
    try:
        return build()
    except Exception as exc:
        log.warning("%s unavailable, Docket will run without it: %s", label, exc)
        return None


def _build_deps(settings: Settings, store: Any) -> pipeline.Deps:
    """Wire the real clients (RFC 10.1). Everything here is injectable for tests."""
    github_client = github_collector = coverage_collector = status_writer = None

    if settings.github_token:
        def _client():
            from github import Auth, Github

            return Github(auth=Auth.Token(settings.github_token))

        github_client = _try("GitHub client", _client)

    if github_client is not None:
        def _collector():
            from docket.collectors.github import GithubCollector

            return GithubCollector(github_client)

        def _coverage():
            from docket.collectors.ci_coverage import CoverageCollector

            return CoverageCollector(
                github_client,
                token=settings.github_token,
                artifact_name=settings.config.github.coverage_artifact_name,
            )

        def _status():
            from docket.record.github_status import GithubStatusWriter

            return GithubStatusWriter(settings, github_client)

        github_collector = _try("Repo Collector", _collector)
        coverage_collector = _try("CI Collector", _coverage)
        status_writer = _try("GitHub status writer", _status)

    def _freshservice():
        from docket.record.freshservice import FreshserviceWriter

        # The store is how the writer finds an existing ticket for this change_key.
        # Without it every re-run would POST a new change instead of PUT-ing (RFC 10.3).
        return FreshserviceWriter(settings, store=store)

    def _brief():
        from docket.record.brief_claude import BoardBrief

        return BoardBrief(settings)

    fs_writer = _try("Freshservice writer", _freshservice)
    brief = _try("Board Brief", _brief) if settings.config.brief.enabled else None

    def _local_git():
        from docket.collectors.local_git import LocalGitCollector

        return LocalGitCollector()

    return pipeline.Deps(
        settings=settings,
        store=store,
        github=github_collector,
        local_git=_try("local git collector", _local_git),
        coverage=coverage_collector,
        freshservice=fs_writer,
        status=status_writer,
        brief=brief,
        mode=settings.mode,
    )


app = None


def get_app() -> FastAPI:
    """uvicorn entry point: `uvicorn docket.server:get_app --factory`."""
    global app
    if app is None:
        app = create_app(background_watcher=True)
    return app

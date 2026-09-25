"""The GitHub commit status writer (RFC 10.4).

Assumptions written down as RFC section 0 rule 2 asks:
  * A `ChangeRecord` carries no head SHA (RFC 6.4), so the pipeline passes one in.
    When it does not, the writer falls back to the newest commit matched by a
    session, then to the commit an approval was given against, and only then asks
    GitHub for `pull.head.sha`.
  * The repository and PR number come from `record.source_url`
    (https://github.com/{owner}/{name}/pull/{n}); `change_key` is not parsed,
    because a repository name may itself contain a hyphen.
  * After an approval sync the state comes from the same
    `config.github.status_states` map (`APPROVE` for approved, `REJECTED` for
    rejected), so no state string is hard-coded (RFC section 0 rule 4).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from docket.models.run import Run

log = logging.getLogger("docket.record.github_status")

MAX_DESCRIPTION = 140
_PR_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")


class GithubStatusWriter:
    """Posts `docket/gate` on the head commit of a pull request."""

    def __init__(self, settings: Any, github_client: Any = None) -> None:
        self.settings = settings
        self.config = settings.config
        self.github = github_client               # a PyGithub Github instance, injected

    # ---- helpers ----------------------------------------------------------

    def target(self, run: Run) -> tuple[str, int] | None:
        """(repo full name, pr number) from the record's source url."""
        match = _PR_URL.search(run.record.source_url or "")
        if not match:
            return None
        owner, name, number = match.groups()
        return f"{owner}/{name}", int(number)

    def state_for(self, run: Run, approval: str | None = None) -> str:
        states = self.config.github.status_states
        if approval == "approved":
            return states.get("APPROVE", "success")
        if approval == "rejected":
            return states.get("REJECTED", "failure")
        return states.get(run.decision.decision, "pending")

    def description_for(self, run: Run) -> str:
        d = run.decision
        if d.decision == "HOLD":
            text = (
                f"HOLD · risk {d.composite:.1f} over limit {d.threshold:g}"
                " · CAB approval requested"
            )
        else:
            text = f"APPROVE · risk {d.composite:.1f} under limit {d.threshold:g}"
        if len(text) > MAX_DESCRIPTION:
            text = text[: MAX_DESCRIPTION - 1] + "…"
        return text

    def target_url(self, run: Run) -> str:
        if run.outputs.freshservice_url:
            return run.outputs.freshservice_url
        base = self.config.server.public_base_url.rstrip("/")
        return f"{base}/#/changes/{run.change_key}"

    def _head_sha(self, run: Run, pull: Any) -> str | None:
        for session in run.record.sessions:
            if session.matched_commits:
                return session.matched_commits[-1]
        if not run.record.review.stale_approval:
            for approver in run.record.review.approvers:
                if approver.approved_commit:
                    return approver.approved_commit
        return getattr(getattr(pull, "head", None), "sha", None)

    # ---- the writer -------------------------------------------------------

    def write(
        self,
        run: Run,
        head_sha: str | None = None,
        approval: str | None = None,
        state: str | None = None,
    ) -> str | None:
        """Post the commit status. Mutates run.outputs.github_status_state.

        Returns the state, or None when it was skipped; a caller that expects
        None is safe. Never raises: a failure lands in run.outputs.errors.
        """
        if run.record.kind != "code":
            log.debug("no commit for a %s change; skipping the status", run.record.kind)
            return None
        try:
            target = self.target(run)
            if target is None:
                self._error(run, "github status skipped: no pull request url on the record")
                return None
            if self.github is None:
                # No GITHUB_TOKEN configured: a skip, not a failure.
                log.debug("github status skipped: no github client")
                return None
            repo_name, pr_number = target
            repo = self.github.get_repo(repo_name)
            sha = head_sha
            if not sha:
                sha = self._head_sha(run, repo.get_pull(pr_number))
            if not sha:
                self._error(run, "github status skipped: no head commit could be resolved")
                return None
            chosen = state or self.state_for(run, approval)
            repo.get_commit(sha).create_status(
                state=chosen,
                target_url=self.target_url(run),
                description=self.description_for(run),
                context=self.config.github.status_context,
            )
            run.outputs.github_status_state = chosen
            return chosen
        except Exception as exc:                        # never fail the run (RFC section 10)
            self._error(run, f"github status failed: {exc.__class__.__name__}: {exc}")
            return None

    def _error(self, run: Run, message: str) -> None:
        log.warning(message)
        run.outputs.errors.append(message)

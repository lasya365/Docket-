"""Unified-diff patch -> hunks (RFC 7.3, "Patch parsing").

`f.patch` from GitHub contains only hunks, no file header.

Assumptions taken here (RFC section 0, rule 2 — simplest reading, written down):
  * `hunk_id` is `"{path}#{index}"` with `index` counted from 0 over the hunks
    of this file (RFC 6.2).
  * Lines before the first `@@` header, and any line the rules do not name
    (a file header a caller passed in by mistake, for instance), are skipped
    and logged at DEBUG. A malformed patch yields the hunks it could read.
  * An empty line inside a patch body is a context line with an omitted space,
    which git emits; it advances `new_line` by one.
  * `old_lines`/`new_lines` keep the counts the header declared (defaulting to
    1 when omitted), not a recount of the body.
"""

from __future__ import annotations

import logging
import re

from docket.models.fragments import DiffLine, Hunk

log = logging.getLogger("docket.collectors.diffparse")

# @@ -a,b +c,d @@ optional section heading
HUNK_HEADER = re.compile(
    r"^@@+\s*-(?P<old_start>\d+)(?:,(?P<old_lines>\d+))?"
    r"\s+\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))?\s*@@"
)

_FILE_HEADER_PREFIXES = ("diff --git ", "index ", "--- ", "+++ ", "old mode ",
                         "new mode ", "similarity index ", "rename from ",
                         "rename to ", "new file mode ", "deleted file mode ",
                         "GIT binary patch", "Binary files ")


def parse_patch(path: str, patch: str | None) -> list[Hunk]:
    """Parse one file's patch into `Hunk`s. `None` or "" gives an empty list."""
    hunks: list[Hunk] = []
    if not patch:
        return hunks

    current: Hunk | None = None
    new_line = 0

    for raw in patch.split("\n"):
        header = HUNK_HEADER.match(raw)
        if header:
            current = Hunk(
                hunk_id=f"{path}#{len(hunks)}",
                path=path,
                old_start=int(header.group("old_start")),
                old_lines=int(header.group("old_lines") or 1),
                new_start=int(header.group("new_start")),
                new_lines=int(header.group("new_lines") or 1),
                added=[],
                removed_count=0,
            )
            hunks.append(current)
            new_line = current.new_start
            continue

        if current is None:
            if raw.strip() and not raw.startswith(_FILE_HEADER_PREFIXES):
                log.debug("patch line before the first hunk header, skipped: %r", raw[:60])
            continue

        if raw.startswith("+"):
            current.added.append(DiffLine(line_no=new_line, text=raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.removed_count += 1
        elif raw.startswith("\\"):
            # "\ No newline at end of file" belongs to the line above it.
            continue
        elif raw.startswith(" ") or raw == "":
            new_line += 1
        else:
            log.debug("unknown patch line, skipped: %r", raw[:60])

    return hunks

#!/bin/bash
# Prints nothing: SessionStart stdout would be added to the agent's context.
input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")
[ -z "$sid" ] && exit 0
root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")

# 1. Make the session id visible to every later Bash tool call.
[ -n "$CLAUDE_ENV_FILE" ] && echo "export DOCKET_SESSION_ID=$sid" >> "$CLAUDE_ENV_FILE"

# 2. Leave it for commits the human makes in their own terminal.
gitdir=$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null)
[ -n "$gitdir" ] && echo "$sid $(date +%s)" > "$gitdir/docket-session"

# 3. Tell Docket where this session lives.
curl -s -m 3 -X POST "$DOCKET_URL/hooks/claude-code" \
  -H "Authorization: Bearer $DOCKET_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -c --arg root "$root" '. + {repo_root: $root}' <<<"$input")" >/dev/null 2>&1 || true
exit 0

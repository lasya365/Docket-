# Installing Docket in a governed repo

These four files are what a repository needs so Docket can see how a change was
really made. They are verbatim from RFC appendix A. Copy them into the repo you
want governed — not into Docket itself.

## Install

From the root of the governed repo:

```bash
# 1. Hooks Claude Code reads (SessionStart + the edit hook).
cp -R /path/to/docket/agent-setup/.claude .

# 2. The git hook that stamps the session id onto every commit.
cp /path/to/docket/agent-setup/prepare-commit-msg .git/hooks/prepare-commit-msg
chmod +x .git/hooks/prepare-commit-msg .claude/hooks/docket_session_start.sh

# 3. The telemetry environment. SOURCE IT BEFORE STARTING claude.
cp /path/to/docket/agent-setup/docket-env.sh .
source ./docket-env.sh
claude
```

Edit `docket-env.sh` first if Docket is not on `http://localhost:8000` or if
`DOCKET_INGEST_TOKEN` is not `change-me`. The same token must be in Docket's
`.env`. If you change the URL, change it in `.claude/settings.json` too: the
`PostToolUse` hook posts to a literal URL.

**`source docket-env.sh` must happen before `claude` starts.** Variables set
after launch have no effect — Claude Code reads the exporter settings once, at
startup. For an organisation rollout the same keys go in the `env` block of
Claude Code's managed settings instead.

`docket_session_start.sh` needs `jq`, `curl` and `git` on the PATH.

### The hook URL must be literal

`$DOCKET_URL/hooks/claude-code` does **not** work. Tested: Claude Code does not expand
environment variables inside a hook's `url`, even when the variable is listed in
`allowedEnvVars` (that list covers the `headers`, where `$DOCKET_INGEST_TOKEN` does work).
The session arrives, the edits silently do not, and every added line is then labelled
`unknown` instead of `ai`.

Write the real URL into `.claude/settings.json`, and remember it is committed to the
governed repo: a `git checkout` or `git reset --hard` will happily restore an old port
over your edit. `make doctor` in Docket compares this URL with `server.public_base_url`
and warns when they disagree.

## What each file does

| File | Installed as | Purpose |
|---|---|---|
| `docket-env.sh` | sourced in the shell | turns on OTLP/HTTP JSON telemetry and points it at Docket |
| `.claude/settings.json` | `.claude/settings.json` | runs the session-start script; posts every Edit/Write/MultiEdit/NotebookEdit to `/hooks/claude-code` |
| `.claude/hooks/docket_session_start.sh` | same path, executable | exports `DOCKET_SESSION_ID`, drops `$GIT_DIR/docket-session`, tells Docket the repo root |
| `prepare-commit-msg` | `.git/hooks/prepare-commit-msg`, executable | adds the `DocketSession-Id:` trailer at commit time |

The edit hook is an HTTP hook, so if Docket is down the failure is
non-blocking: Claude Code carries on.

## Checking it works

1. **Telemetry is being exported.** Start the agent with `claude --debug` and
   look for `[3P telemetry]` lines. No such line means the environment was not
   sourced before launch.
2. **Docket is receiving it.** With Docket running, `GET /sessions` lists the
   session. It should carry prompts, tool names and edits:

   ```bash
   curl -s -H "Authorization: Bearer $DOCKET_INGEST_TOKEN" \
     http://localhost:8000/sessions | jq '.[0] | {session_id, turns, detail}'
   ```

   `detail.prompts`, `detail.tool_details` and `detail.edits` should all be
   `true`. A `false` means one of the three wires is not connected:
   `prompts` → `OTEL_LOG_USER_PROMPTS`; `tool_details` → `OTEL_LOG_TOOL_DETAILS`;
   `edits` → the `PostToolUse` hook.
3. **The trailer is being stamped.** Make a commit from inside the session and
   run `git log -1 --format=%B`. It should end with `DocketSession-Id: <id>`.
4. **Nothing arrives at all?** Check the token matches Docket's
   `DOCKET_INGEST_TOKEN` (a mismatch is a `401`) and that
   `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` is set (Docket answers `415` with
   `Set OTEL_EXPORTER_OTLP_PROTOCOL=http/json` otherwise).

The commit SHA on a tool result and the `vcs.*` attributes need Claude Code
v2.1.269 or later; `prompt_id` in hook payloads needs v2.1.196 or later. Docket
works without any of them, with less evidence.


---

## Windows

Everything works on Windows, with three differences. All three have bitten someone.

### 1. The hook must invoke `bash` explicitly

`.claude/settings.json` ships this, and it is deliberate:

```json
"command": "bash \"${CLAUDE_PROJECT_DIR}/.claude/hooks/docket_session_start.sh\""
```

Do **not** rewrite it to the bare script path, and do **not** add an `args` array.
Adding `args` switches Claude Code from *shell form* to *exec form*: it then spawns the
file directly through libuv, and Windows cannot execute a text file. The symptom is:

```
EFTYPE: inappropriate file type or format, uv_spawn
```

With no `args`, the command string goes to a shell (Git Bash when it is on PATH), and
`bash "<path>"` runs the script the same way on Windows, macOS and Linux. A bare `.sh`
path on Windows is resolved by *file association* rather than by its shebang, which
launches `git-bash.exe` in a separate window that does not inherit stdio — so even when
it appears to run, Docket receives nothing.

If `bash` is not found, Git for Windows is either not installed or not on PATH. Note
that `C:\Windows\System32\bash.exe` is the WSL stub, not Git Bash; either put Git's
`bin` directory on PATH or set `CLAUDE_CODE_GIT_BASH_PATH` to the real `bash.exe`.

### 2. Line endings

The hook is a shell script. If Git rewrites it with CRLF, bash looks for an interpreter
called `bash\r` and fails with a confusing error. `agent-setup/.gitattributes` pins
`*.sh` and `prepare-commit-msg` to LF — copy it into the governed repo along with the
hooks, or set `git config core.autocrlf false` there.

### 3. Environment variables

`export VAR=value` is bash syntax and does nothing in PowerShell or cmd. Use the shipped
scripts instead, and run them **in the same window** you then start `claude` from:

```powershell
. .\docket-env.ps1     # PowerShell — the leading dot matters
claude
```

```cmd
docket-env.cmd          :: cmd.exe
claude
```

The git hook (`prepare-commit-msg`) needs no change: Git for Windows runs its hooks under
its own bash.

### Checking it worked on Windows

```powershell
$env:DOCKET_INGEST_TOKEN          # should print your token, not blank
claude --debug                    # look for [3P telemetry] lines
```

Then, on the Docket side, `make doctor` reports whether any session arrived, whether it
carried edit text, and whether prompts and tool details were included.

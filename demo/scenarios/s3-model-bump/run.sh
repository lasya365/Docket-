#!/usr/bin/env bash
# S3 — support agent model 4.1 -> 4.2 (kind: model_version).
#
# A change with no diff at all. This script edits demo/agent.manifest.json,
# asks the Manifest Watcher to look, and prints the change record it filed.
#
# Usage:
#   ./run.sh              apply the bump and show the record
#   ./run.sh --reset      put the manifest back to vendor-4.1
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PY="$ROOT/.venv/bin/python"
MANIFEST="$ROOT/demo/agent.manifest.json"
BACKUP="$HERE/.manifest.before.json"
BASELINE="$ROOT/demo/agent.manifest.baseline.json"
DOCKET_URL="${DOCKET_URL:-http://localhost:8000}"

AUTH=()
if [ -n "${DOCKET_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${DOCKET_API_TOKEN}")
fi

[ -x "$PY" ] || { echo "no venv at $PY — run 'make install' from $ROOT" >&2; exit 1; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

check_running() {
  if ! curl -sf --max-time 5 "$DOCKET_URL/healthz" >/dev/null; then
    echo "Docket is not answering on $DOCKET_URL." >&2
    echo "Start it first:  cd $ROOT && make serve" >&2
    exit 1
  fi
  curl -s "$DOCKET_URL/healthz" | "$PY" -m json.tool
}

# POST /manifest/check and echo the change keys it filed, one per line.
watch() {
  curl -s -X POST ${AUTH[@]+"${AUTH[@]}"} "$DOCKET_URL/manifest/check" \
    | "$PY" -c 'import json,sys
for row in json.load(sys.stdin):
    print(row["change_key"])'
}

if [ "${1:-}" = "--reset" ]; then
  say "S3 reset — restoring demo/agent.manifest.json"
  # Surgical on purpose: restore ONLY this scenario's key, from the shipped baseline.
  # A whole-file restore is wrong here - if S4 ran after S3, its backup contains S3's
  # bump, so restoring it would silently re-apply a change the operator just undid.
  "$PY" - "$MANIFEST" "$BASELINE" <<'RESET'
import json, sys
path, baseline = sys.argv[1], sys.argv[2]
body = json.loads(open(path).read())
base = json.loads(open(baseline).read())
body["model"] = base["model"]
open(path, "w").write(json.dumps(body, indent=2) + "\n")
print("model restored to", base["model"], "from the baseline")
RESET
  rm -f "$BACKUP"
  if curl -sf --max-time 5 "$DOCKET_URL/healthz" >/dev/null; then
    say "Re-syncing the ledger (the revert is itself a change Docket files)"
    watch | sed 's/^/  filed: /'
  fi
  echo
  echo "To clear the board as well:  cd $ROOT && make reset"
  exit 0
fi

say "S3 — support agent model 4.1 -> 4.2"
check_running

say "1. Before state"
cp "$MANIFEST" "$BACKUP"
"$PY" - "$MANIFEST" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "backend" / "src"))
from docket.collectors.watchers.manifest import load_manifest
manifest, digest = load_manifest(Path(sys.argv[1]))
print(f"  agent      {manifest.agent_id}")
print(f"  model      {manifest.model}")
print(f"  workflows  {', '.join(manifest.workflows)}  ({len(manifest.workflows)} flows)")
print(f"  bundle     {digest[:12]}")
PY

say "2. Baselining the ledger (a first sighting files nothing, by design)"
watch | sed 's/^/  filed: /' || true

say "3. Editing demo/agent.manifest.json:  \"model\": \"vendor-4.1\" -> \"vendor-4.2\""
"$PY" - "$MANIFEST" "$ROOT" <<'EDIT'
import json, sys
from pathlib import Path
root = Path(sys.argv[2])
sys.path.insert(0, str(root / "backend" / "src"))
from docket.collectors.watchers.manifest import load_manifest, manifest_hash, rehearsal_path

path = Path(sys.argv[1])
body = json.loads(path.read_text())
body["model"] = "vendor-4.2"
# S3 is told as "no issue, no PRD", so the intent link must read MISSING. S4 leaves
# a change_ref behind; clear it. change_ref is excluded from the bundle hash
# (RFC 7.5 step 2), so clearing it never files a change of its own.
stale = body.get("change_ref")
if stale is not None:
    print("  note       cleared a leftover change_ref (" + str(stale) + ") from another scenario")
    body["change_ref"] = None
path.write_text(json.dumps(body, indent=2) + "\n")

manifest, digest = load_manifest(path)
print("  model      " + manifest.model)
print("  bundle     " + digest[:12])
corpus = rehearsal_path(root / "demo" / "rehearsal", manifest.agent_id, digest)
if corpus.is_file():
    reh = json.loads(corpus.read_text())
    print("  rehearsal  " + corpus.name)
    print("             {} cases | {} identical | {} changed acceptably | {} regressed ({})".format(
        reh["cases"], reh["identical"], reh["changed_acceptable"], reh["regressed"], reh["provenance"]))
    for r in reh["regressions"]:
        print("             {}  {}  ->  {}".format(r["case_id"], r["before"], r["after"]))
else:
    print("  !! no rehearsal corpus at " + corpus.name)
    print("     The bundle hash moved. Regenerate the file name with manifest_hash(),")
    print("     or the Verify link will read: none, nothing verified.")
EDIT

say "4. POST /manifest/check"
KEYS="$(watch)"
if [ -z "$KEYS" ]; then
  echo "  nothing filed — the ledger already held this bundle." >&2
  echo "  Run './run.sh --reset', then './run.sh' again." >&2
  exit 1
fi
echo "$KEYS" | sed 's/^/  change_key: /'

TMP="$(mktemp -t docket-s3)"
trap 'rm -f "$TMP"' EXIT

for KEY in $KEYS; do
  say "5. The record — GET /changes/$KEY"
  curl -s ${AUTH[@]+"${AUTH[@]}"} "$DOCKET_URL/changes/$KEY" > "$TMP"
  "$PY" - "$TMP" <<'SHOW'
import json, sys

run = json.loads(open(sys.argv[1]).read())
d = run["decision"]
print("  change_key  " + run["change_key"])
print("  kind        " + run["record"]["kind"])
print("  title       " + run["record"]["title"])
print("  decision    {}   composite {}  (threshold {}, risk {})".format(
    d["decision"], d["composite"], d["threshold"], d["risk_level"]))
for hs in d["hard_stops_fired"]:
    print("  hard stop   {}: {}".format(hs.get("id"), hs.get("reason", "")))
print()
print("  signals")
for sig in run["signals"]:
    print("    {:<24}{:>7}  {:<10}{}".format(
        sig["label"], sig["score"], sig["status"], sig["headline"]))
print()
chain = run["chain"]
print("  evidence chain - {} of {} links MISSING".format(
    chain["missing_count"], len(chain["links"])))
for link in chain["links"]:
    print("  {} {:<9}{}".format("ok  " if link["present"] else "**  ",
                                link["name"], link["summary"]))
missing = [l["name"] for l in chain["links"] if not l["present"]]
print("  missing: " + (", ".join(missing) if missing else "none"))
print()
print("  backout plan (available={})".format(run["backout"]["available"]))
for line in (run["backout"]["text"] or "").splitlines():
    print("    " + line)
print()
print("  rollout: " + (run["rollout_text"] or "none"))
SHOW
done

say "Say it out loud"
echo '  "The resolved ticket is the label."'
echo
echo "Reset with:  $HERE/run.sh --reset"

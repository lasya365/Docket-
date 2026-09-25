#!/usr/bin/env bash
# S4 — knowledge article rewrite, kb-2211 revision 7 -> 8 (kind: knowledge).
#
# A Freshservice solution article, "VPN certificate errors", that three agents
# retrieve from. The step order changes and a workaround is removed. Nobody
# reviews it; it is live the moment it is saved.
#
# Docket sees it because the article is pinned in the agent bundle
# (demo/agent.manifest.json -> knowledge), so bumping the revision travels the
# same Manifest Watcher as every other no-code change (RFC 7.5). No Freshservice
# account is needed: the two revisions ship as files under demo/knowledge/.
#
# Usage:
#   ./run.sh              publish revision 8 and show the record
#   ./run.sh --reset      put kb-2211 back to revision 7
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

watch() {
  curl -s -X POST ${AUTH[@]+"${AUTH[@]}"} "$DOCKET_URL/manifest/check" \
    | "$PY" -c 'import json,sys
for row in json.load(sys.stdin):
    print(row["change_key"])'
}

if [ "${1:-}" = "--reset" ]; then
  say "S4 reset — kb-2211 back to revision 7"
  # Surgical on purpose: restore ONLY this scenario's key, from the shipped baseline.
  # A whole-file restore would re-apply whatever another scenario had changed when
  # this one's backup was taken.
  "$PY" - "$MANIFEST" "$BASELINE" <<'RESET'
import json, sys
path, baseline = sys.argv[1], sys.argv[2]
body = json.loads(open(path).read())
base = json.loads(open(baseline).read())
body["knowledge"] = base["knowledge"]
# also the ticket reference this scenario stamps on; it is excluded from the bundle
# hash so it files nothing, but leaving it behind makes the next diff read oddly.
body["change_ref"] = base.get("change_ref")
open(path, "w").write(json.dumps(body, indent=2) + "\n")
print("knowledge restored to", [(k["id"], k["revision"]) for k in base["knowledge"]], "from the baseline")
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

say "S4 — knowledge article rewrite: kb-2211 'VPN certificate errors', revision 7 -> 8"

if ! curl -sf --max-time 5 "$DOCKET_URL/healthz" >/dev/null; then
  echo "Docket is not answering on $DOCKET_URL." >&2
  echo "Start it first:  cd $ROOT && make serve" >&2
  exit 1
fi
curl -s "$DOCKET_URL/healthz" | "$PY" -m json.tool

say "1. Before state — what the agent bundle pins"
cp "$MANIFEST" "$BACKUP"
"$PY" - "$MANIFEST" "$ROOT" <<'BEFORE'
import sys
from pathlib import Path
root = Path(sys.argv[2])
sys.path.insert(0, str(root / "backend" / "src"))
from docket.collectors.watchers.manifest import load_manifest
manifest, digest = load_manifest(Path(sys.argv[1]))
print(f"  agent      {manifest.agent_id}")
print(f"  knowledge  " + ", ".join(f"{k.id} r{k.revision}" for k in manifest.knowledge))
print(f"  workflows  {', '.join(manifest.workflows)}  ({len(manifest.workflows)} flows)")
print(f"  bundle     {digest[:12]}")
BEFORE

say "2. Baselining the ledger (a first sighting files nothing, by design)"
watch | sed 's/^/  filed: /' || true

say "3. What the support agent did — demo/knowledge/kb-2211.r7.md -> kb-2211.r8.md"
diff -u "$ROOT/demo/knowledge/kb-2211.r7.md" "$ROOT/demo/knowledge/kb-2211.r8.md" \
  | sed 's/^/  /' || true

say "4. Who reads it (SIMULATED mapping — demo/knowledge/retrieval-map.json)"
"$PY" - "$ROOT/demo/knowledge/retrieval-map.json" <<'MAP'
import json, sys
body = json.loads(open(sys.argv[1]).read())
print(f"  provenance  {body['provenance']}")
article = body["articles"]["kb-2211"]
print(f"  retrieved by {len(article['retrieved_by'])} agents:")
for row in article["retrieved_by"]:
    print(f"    {row['agent_id']:<22}{row['why_it_retrieves']}")
for row in article["contradicts"]:
    print(f"  contradicts {row['article_id']} ({row['status']})")
    print(f"    {row['why']}")
MAP

say "5. Publishing revision 8 into demo/agent.manifest.json"
"$PY" - "$MANIFEST" "$ROOT" <<'EDIT'
import json, sys
from pathlib import Path
root = Path(sys.argv[2])
sys.path.insert(0, str(root / "backend" / "src"))
from docket.collectors.watchers.manifest import load_manifest

path = Path(sys.argv[1])
body = json.loads(path.read_text())
for ref in body.get("knowledge", []):
    if ref["id"] == "kb-2211":
        ref["revision"] = "8"
# The support ticket that asked for a refresh. It is not a change request, and it
# is excluded from the bundle hash, so it never files a change record of its own.
body["change_ref"] = "INC-4390"
path.write_text(json.dumps(body, indent=2) + "\n")

manifest, digest = load_manifest(path)
print("  knowledge  " + ", ".join(f"{k.id} r{k.revision}" for k in manifest.knowledge))
print(f"  change_ref {manifest.change_ref}  (a support ticket, not a change request)")
print(f"  bundle     {digest[:12]}")
EDIT

say "6. POST /manifest/check"
KEYS="$(watch)"
if [ -z "$KEYS" ]; then
  echo "  nothing filed — the ledger already held this bundle." >&2
  echo "  Run './run.sh --reset', then './run.sh' again." >&2
  exit 1
fi
echo "$KEYS" | sed 's/^/  change_key: /'

TMP="$(mktemp -t docket-s4)"
trap 'rm -f "$TMP"' EXIT

for KEY in $KEYS; do
  say "7. The record — GET /changes/$KEY"
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
echo '  "Someone with permission to fix a typo just changed how every VPN incident'
echo '   gets resolved. No ITSM tool filed a record for it. Docket did — and it'
echo '   wrote the backout plan that never existed."'
echo
echo "Reset with:  $HERE/run.sh --reset"

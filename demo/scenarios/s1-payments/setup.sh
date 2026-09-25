#!/usr/bin/env bash
# Create a fresh scenario repo for REQ-114 from the template next to this script.
#
# This script NEVER runs `claude`. It only stages the repository; you drive the
# agent session yourself, so the telemetry Docket collects is real.
#
#   ./setup.sh [TARGET_DIR] [--with-agent-setup]
#
# TARGET_DIR defaults to ~/docket-demo/s1-payments
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKET_ROOT="$(cd "$HERE/../../.." && pwd)"
TEMPLATE="$HERE/template"
BRANCH="feature/req-114-webhook-retry"

TARGET="${HOME}/docket-demo/s1-payments"
WITH_AGENT_SETUP=0
for arg in "$@"; do
  case "$arg" in
    --with-agent-setup) WITH_AGENT_SETUP=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) TARGET="$arg" ;;
  esac
done

if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "refusing to write into a non-empty $TARGET" >&2
  echo "remove it first:  rm -rf '$TARGET'" >&2
  exit 1
fi

mkdir -p "$TARGET"
# -a keeps dotfiles (.gitignore); the trailing /. copies contents, not the dir
cp -R "$TEMPLATE/." "$TARGET/"
find "$TARGET" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$TARGET" -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true

if [ "$WITH_AGENT_SETUP" -eq 1 ]; then
  if [ -d "$DOCKET_ROOT/agent-setup" ]; then
    cp -R "$DOCKET_ROOT/agent-setup/.claude" "$TARGET/"
    cp "$DOCKET_ROOT/agent-setup/docket-env.sh" "$TARGET/"
    chmod +x "$TARGET/.claude/hooks/"*.sh 2>/dev/null || true
    echo "copied agent-setup/ telemetry files -- edit $TARGET/docket-env.sh before sourcing it"
  else
    echo "warning: $DOCKET_ROOT/agent-setup not found, skipping" >&2
  fi
fi

cd "$TARGET"
git init -q -b main
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-Docket Demo}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-demo@example.invalid}" \
    commit -q -m "Baseline: storefront payments service"

if [ "$WITH_AGENT_SETUP" -eq 1 ] && [ -f "$DOCKET_ROOT/agent-setup/prepare-commit-msg" ]; then
  cp "$DOCKET_ROOT/agent-setup/prepare-commit-msg" .git/hooks/prepare-commit-msg
  chmod +x .git/hooks/prepare-commit-msg
fi

git checkout -q -b "$BRANCH"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' 2>/dev/null; then
  python3 -m pytest -q
else
  echo "pytest not importable with python3 -- run the tests yourself before the demo"
fi

cat <<BANNER

  ready:  $TARGET
  branch: $BRANCH  (baseline committed on main)

  next:   cd "$TARGET"
          source ./docket-env.sh     # if you passed --with-agent-setup
          claude

  the prompts are in $HERE/PROMPTS.md
BANNER

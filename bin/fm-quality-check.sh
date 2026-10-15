#!/usr/bin/env bash
# fm-quality-check.sh - deterministic per-project Crapper completion gate.
#
# Usage: fm-quality-check.sh ensure|status|rule|run <task-id>
#        fm-quality-check.sh scan
# ensure starts one detached bounded runner, or returns the current receipt.
# status only reads: it never starts tests or writes state. run performs an
# explicit foreground retry. Zero means passed or not configured; one means
# pending, failed, stale, or unavailable. rule also only reads, and answers
# whether a project rule applies to the task at all: zero means one applies,
# three means none does (no config, no rule for the project, or not a ship),
# and any other status means the config or task metadata could not be read.
# scan ensures every ship with a current
# done declaration and is called by every normal watcher poll, without model
# involvement. It uses the shared status reconciler, never raw tail heuristics.
#
# docs/configuration.md owns config/project-quality.json. Checks execute only
# in the isolated task copy. The interpreter must contain the pinned Crapper
# package; the adapter uses its discovery, parser, coverage, and scoring APIs.
# The configured coverage command runs first and must succeed and recreate all
# declared reports. Source and test inputs must be committed before validation.
# Crapper then scores functions touched since quality_base,
# captured before launch. Receipts bind task incarnation, base, HEAD, tracked
# edits, untracked inputs (excluding generated reports, coverage outputs, and
# caches), and the
# selected project rule. Changed code invalidates
# a pass; changes during validation refuse publication. Failed attempts are
# cached until edits or an explicit run retry, preventing repeated model wakes.
# Polling waits for a new completion declaration before checking repaired code;
# an old done event never starts another run while the agent is editing.
# A failure is sent once through fm-send's durable inbox and retains its report.
# state/quality/ retains private receipts, logs, scored entries, and requests.
# A removed/replaced task receives no late feedback. This is a local correctness
# gate, not protection against a malicious agent with the supervisor's OS access.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}}"
STATE="${FM_STATE_OVERRIDE:-$FM_HOME/state}"
CONFIG="${FM_CONFIG_OVERRIDE:-$FM_HOME/config}"
case "${1:-}" in
  -h|--help) sed -n '2,${/^#/!q;p;}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  ensure|status|rule|run) [ "$#" -eq 2 ] || exit 2 ;;
  scan) [ "$#" -eq 1 ] || exit 2 ;;
  *) exit 2 ;;
esac
if [ ! -e "$CONFIG/project-quality.json" ] && [ ! -L "$CONFIG/project-quality.json" ]; then
  [ "$1" = rule ] && exit 3
  exit 0
fi
command -v python3 >/dev/null 2>&1 || { echo 'quality: python3 is required'; exit 1; }
if [ "$1" = scan ]; then
  # shellcheck source=bin/fm-classify-lib.sh
  . "$SCRIPT_DIR/fm-classify-lib.sh"
  for meta in "$STATE"/*.meta; do
    [ -f "$meta" ] && [ ! -L "$meta" ] || continue
    kind=$(sed -n 's/^kind=//p' "$meta")
    [ "$kind" = ship ] || continue
    id=$(basename "$meta" .meta)
    line=$(status_current_line "$STATE/$id.status" "$kind")
    [ "$(status_line_verb "$line")" = "done" ] || continue
    FM_HOME="$FM_HOME" FM_QUALITY_FROM_POLL=1 python3 "$SCRIPT_DIR/fm-quality-check.py" ensure "$id" >/dev/null || true
  done
  exit 0
fi
FM_HOME="$FM_HOME" exec python3 "$SCRIPT_DIR/fm-quality-check.py" "$@"

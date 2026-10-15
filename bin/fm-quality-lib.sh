#!/usr/bin/env bash
# Read-only receipt gate used by completion, publication, and landing.
# bin/fm-quality-check.sh owns execution and receipt identity; this file only
# selects the task's captured metadata and optional forge-reported head.
fm_quality_accept() {  # <state> <id> [meta] [expected-head]
  local state=${1:-} id=${2:-} meta=${3:-} head=${4:-} directory config home reason require_head=0
  [ "$#" -lt 4 ] || require_head=1
  directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  home=${FM_HOME:-${FM_ROOT_OVERRIDE:-$(cd "$directory/.." && pwd)}}
  config=${FM_CONFIG_OVERRIDE:-$home/config}
  [ ! -e "$config/project-quality.json" ] && [ ! -L "$config/project-quality.json" ] && return 0
  if [ -z "$state" ] || [ -z "$id" ]; then
    printf '%s\n' 'quality: task identity is required to verify readiness'
    return 1
  fi
  if reason=$(FM_HOME="$home" FM_STATE_OVERRIDE="$state" FM_QUALITY_META_OVERRIDE="$meta" \
    FM_QUALITY_REQUIRE_HEAD="$require_head" FM_QUALITY_EXPECTED_HEAD="$head" "$directory/fm-quality-check.sh" status "$id"); then
    return 0
  fi
  printf '%s\n' "${reason:-quality: receipt could not be verified}"
  return 1
}

#!/usr/bin/env bash
# Read-only receipt gate used by completion, publication, and landing, plus the
# read-only question of whether a project rule applies to a task at all.
# bin/fm-quality-check.sh owns execution, receipt identity, and rule lookup;
# this file only selects the task's captured metadata and optional
# forge-reported head.
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

# Zero when a project quality rule applies to the task, or when the config or
# task metadata cannot be read to rule one out; one only when no rule applies.
# Callers use it to keep the landing paths a receipt cannot cover closed for
# configured projects while leaving every other project's behavior unchanged.
fm_quality_rule_applies() {  # <state> <id> [meta]
  local state=${1:-} id=${2:-} meta=${3:-} directory config home rc=0
  directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  home=${FM_HOME:-${FM_ROOT_OVERRIDE:-$(cd "$directory/.." && pwd)}}
  config=${FM_CONFIG_OVERRIDE:-$home/config}
  [ ! -e "$config/project-quality.json" ] && [ ! -L "$config/project-quality.json" ] && return 1
  [ -n "$state" ] && [ -n "$id" ] || return 0
  FM_HOME="$home" FM_STATE_OVERRIDE="$state" FM_QUALITY_META_OVERRIDE="$meta" \
    "$directory/fm-quality-check.sh" rule "$id" >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 3 ] && return 1
  return 0
}

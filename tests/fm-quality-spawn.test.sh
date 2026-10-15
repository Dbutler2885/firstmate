#!/usr/bin/env bash
set -eu
# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

TMP_ROOT=$(fm_test_tmproot fm-quality-spawn)
home="$TMP_ROOT/home"
proj="$TMP_ROOT/project"
wt="$TMP_ROOT/wt"
id=quality-baseline-a1
fm_test_spawn_home "$home" codex
fm_git_worktree "$proj" "$wt" task
fm_test_spawn_brief "$home" "$id"
fakebin=$(fm_test_make_spawn_fakebin "$TMP_ROOT/fake")
cat > "$fakebin/tmux" <<'SH'
#!/usr/bin/env bash
case "$1" in
  list-windows) printf '%s\n' "${FM_TEST_WINDOWS:-}" ;;
  display-message)
    case "$*" in
      *pane_current_path*) printf '%s\n' "$FM_FAKE_PANE_PATH" ;;
      *pane_current_command*) printf 'bash\n' ;;
      *pane_pid*) printf '\n' ;;
      *) printf 'firstmate\n' ;;
    esac
    ;;
esac
exit 0
SH
chmod +x "$fakebin/tmux"
out=$(fm_test_run_spawn "$home" "$wt" "$fakebin" "$id" "$proj" --mode local-only --yolo off) \
  || fail "fresh launch failed: $out"
baseline=$(git -C "$wt" rev-parse HEAD)
assert_equals "$baseline" "$(sed -n 's/^quality_base=//p' "$home/state/$id.meta")" \
  "fresh launch must capture its initial baseline"
printf 'task change\n' > "$wt/task-change"
git -C "$wt" add task-change
git -C "$wt" commit -qm 'task change'
printf 'quality_base=%s\nquality_base=%s\n' "$(git -C "$wt" rev-parse HEAD)" "$baseline" \
  >> "$home/state/$id.meta"

out=$(FM_TEST_WINDOWS="fm-$id" fm_test_run_spawn "$home" "$wt" "$fakebin" "$id" --relaunch) \
  || fail "relaunch failed: $out"
assert_equals "$baseline" "$(sed -n 's/^quality_base=//p' "$home/state/$id.meta")" \
  "relaunch must preserve exactly one original baseline despite task commits"

sed '/^quality_base=/d' "$home/state/$id.meta" > "$home/state/legacy.meta"
mv "$home/state/legacy.meta" "$home/state/$id.meta"
out=$(FM_TEST_WINDOWS="fm-$id" fm_test_run_spawn "$home" "$wt" "$fakebin" "$id" --relaunch) \
  || fail "legacy relaunch failed: $out"
assert_equals '' "$(sed -n 's/^quality_base=//p' "$home/state/$id.meta")" \
  "legacy relaunch must preserve its missing baseline"
printf '{"version":1,"projects":{"project":{"crapper_python":"/bin/sh","coverage_command":["true"],"coverage_reports":["coverage/lcov.info"]}}}\n' \
  > "$home/config/project-quality.json"
for action in run ensure status; do
  status=0
  out=$(FM_HOME="$home" FM_STATE_OVERRIDE="$home/state" FM_CONFIG_OVERRIDE="$home/config" \
    "$ROOT/bin/fm-quality-check.sh" "$action" "$id" 2>&1) || status=$?
  expect_code 1 "$status" "legacy relaunch must refuse quality scoring ($action)"
  assert_contains "$out" 'Missing quality_base' "quality scoring must explain the absent baseline ($action)"
done
pass "fresh launches capture a baseline and relaunches preserve its value or absence"

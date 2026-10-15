import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(sys.argv.pop(1))
REAL_PYTHON = os.environ.get('FM_TEST_CRAPPER_PYTHON')


class QualityGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fm-quality-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        for part in ('state', 'config', 'bin'):
            (self.home / part).mkdir(parents=True)
        for name in ('fm-quality-check.sh', 'fm-quality-check.py', 'fm-quality-evaluate.py', 'fm-timeout-lib.sh'):
            shutil.copy2(REPO / 'bin' / name, self.home / 'bin' / name)
        # The command boundary is exercised with a controllable checker; actual
        # Crapper is tested separately when its pinned interpreter is supplied.
        fake_interpreter = self.home / 'fake-crapper-python'
        fake_interpreter.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
r=json.loads(Path(sys.argv[2]).read_text())
assert r['threshold']==7
Path(r['output']).write_text(json.dumps([{'name':'fixture','crap':float(os.environ.get('TEST_SCORE','1'))}]))
print('fixture CRAP report')
sys.exit(2 if float(os.environ.get('TEST_SCORE','1'))>7 else 0)
''')
        fake_interpreter.chmod(0o755)
        sender = self.home / 'bin/fm-send.sh'
        sender.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$FM_HOME/state/feedback"\n')
        sender.chmod(0o755)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.git(self.project, 'init', '-q', '-b', 'main')
        self.git(self.project, 'config', 'user.email', 'test@example.invalid')
        self.git(self.project, 'config', 'user.name', 'test')
        (self.project / 'source.py').write_text('def simple():\n    return 1\n')
        self.git(self.project, 'add', '.')
        self.git(self.project, 'commit', '-qm', 'base')
        self.base = self.git(self.project, 'rev-parse', 'HEAD').stdout.strip()
        self.copy = self.root / 'copy'
        self.git(self.project, 'worktree', 'add', '-qb', 'task', str(self.copy))
        (self.copy / 'source.py').write_text('def simple():\n    return 2\n')
        self.git(self.copy, 'commit', '-qam', 'change')
        self.meta = self.home / 'state/task.meta'
        self.meta.write_text(f'kind=ship\nmode=local-only\nproject={self.project}\nworktree={self.copy}\nquality_base={self.base}\nspawn_gen=s1.test\n')
        self.report = self.copy / 'coverage/lcov.info'
        self.coverage = self.home / 'coverage.py'
        self.coverage.write_text('''import os,time
from pathlib import Path
root=Path.cwd(); home=Path(os.environ['FM_HOME'])
count=home/'state/count'
count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
assert not (root/os.environ.get('TEST_REPORT','coverage/lcov.info')).exists(), 'stale report survived'
time.sleep(float(os.environ.get('TEST_DELAY','0')))
if os.environ.get('TEST_EDIT'):
    (root/'source.py').write_text('def simple():\\n    return 99\\n')
if os.environ.get('TEST_CACHE'):
    (root/'__pycache__').mkdir(exist_ok=True)
    (root/'__pycache__/source.pyc').write_text(str(time.time()))
    (root/'.coverage').write_text(str(time.time()))
if not os.environ.get('TEST_MISSING'):
    p=root/os.environ.get('TEST_REPORT','coverage/lcov.info');p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text('SF:source.py\\n' + ''.join(f'DA:{i},1\\n' for i in range(1,len((root/'source.py').read_text().splitlines())+1)) + 'end_of_record\\n')
raise SystemExit(int(os.environ.get('TEST_COVERAGE_EXIT','0')))
''')
        self.rule = {'crapper_python': str(fake_interpreter), 'coverage_command': [sys.executable, str(self.coverage)], 'coverage_reports': ['coverage/lcov.info'], 'threshold': 7, 'timeout_seconds': 10}
        self.config = self.home / 'config/project-quality.json'
        self.write_config()
        self.env = dict(os.environ, FM_HOME=str(self.home), FM_ROOT_OVERRIDE=str(REPO), FM_STATE_OVERRIDE='', FM_CONFIG_OVERRIDE='')

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)

    def write_config(self):
        self.config.write_text(json.dumps({'version': 1, 'projects': {'project': self.rule}}))

    def call(self, action, **env):
        return subprocess.run([str(self.home / 'bin/fm-quality-check.sh'), action, 'task'], env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def receipt(self):
        return json.loads((self.home / 'state/quality/task.json').read_text())

    def test_unconfigured_is_read_only_noop(self):
        self.config.unlink()
        self.meta.unlink()
        self.assertEqual(self.call('ensure').returncode, 0)
        self.assertFalse((self.home / 'state/quality').exists())

    def test_fresh_coverage_pass_and_cached_receipt(self):
        self.report.parent.mkdir()
        self.report.write_text('stale coverage')
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)
        self.assertEqual(self.call('ensure').returncode, 0)
        self.assertEqual((self.home / 'state/count').read_text(), '1')
        self.assertFalse((self.home / 'state/feedback').exists())

    def test_failed_coverage_is_not_a_quality_pass(self):
        self.assertEqual(self.call('run', TEST_COVERAGE_EXIT='1').returncode, 1)
        self.assertIn('coverage/tests exited 1', self.receipt()['reason'])
        self.assertFalse(Path(self.receipt()['entries']).exists())
        self.assertEqual(self.call('status').returncode, 1)

    def test_missing_report_blocks(self):
        self.assertEqual(self.call('run', TEST_MISSING='1').returncode, 1)
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_threshold_failure_feedback_once_and_repair(self):
        self.assertEqual(self.call('run', TEST_SCORE='8').returncode, 1)
        feedback = (self.home / 'state/feedback').read_text()
        self.assertIn('mandatory quality check for HEAD', feedback)
        self.assertEqual(self.call('ensure').returncode, 1)
        self.assertEqual((self.home / 'state/count').read_text(), '1')
        self.assertEqual((self.home / 'state/feedback').read_text(), feedback)
        (self.copy / 'source.py').write_text('def simple():\n    return 3\n')
        self.git(self.copy, 'commit', '-qam', 'repair')
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)

    def test_any_edit_invalidates_pass(self):
        self.assertEqual(self.call('run').returncode, 0)
        (self.copy / 'source.py').write_text('def simple():\n    return 4\n')
        self.assertEqual(self.call('status').returncode, 1)
        (self.copy / 'source.py').write_text('def simple():\n    return 2\n')
        self.assertEqual(self.call('status').returncode, 0)
        (self.copy / 'new.py').write_text('uncommitted new code')
        self.assertEqual(self.call('status').returncode, 1)

    def test_regenerated_caches_do_not_invalidate_source_identity(self):
        self.assertEqual(self.call('run', TEST_CACHE='1').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)
        self.assertEqual(self.call('run', TEST_CACHE='1').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)

    def test_new_source_beside_report_invalidates_pass(self):
        self.rule['coverage_reports'] = ['backend/lcov.info']
        self.write_config()
        self.env['TEST_REPORT'] = 'backend/lcov.info'
        self.assertEqual(self.call('run').returncode, 0)
        (self.copy/'backend/new.py').write_text('def new():\n    return 1\n')
        self.assertEqual(self.call('status').returncode, 1)

    def test_interpreter_that_produces_no_metrics_cannot_pass(self):
        self.rule['crapper_python'] = shutil.which('true')
        self.write_config()
        self.assertEqual(self.call('run').returncode, 1)
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_configuration_change_invalidates_pass(self):
        self.assertEqual(self.call('run').returncode, 0)
        self.rule['timeout_seconds'] = 11
        self.write_config()
        self.assertEqual(self.call('status').returncode, 1)

    def test_code_changed_during_tests_is_rejected(self):
        self.assertEqual(self.call('run', TEST_EDIT='1').returncode, 1)
        self.assertEqual(self.receipt()['status'], 'failed')
        self.assertFalse((self.home / 'state/feedback').exists())

    def test_timeout_is_failure(self):
        self.rule['timeout_seconds'] = 1
        self.write_config()
        self.assertEqual(self.call('run', TEST_DELAY='3').returncode, 1)
        self.assertIn('exited 124', self.receipt()['reason'])

    def test_primary_copy_cannot_be_checked(self):
        self.meta.write_text(self.meta.read_text().replace(str(self.copy), str(self.project)))
        self.assertEqual(self.call('run').returncode, 1)
        self.assertFalse((self.home / 'state/count').exists())

    def test_symlinked_report_does_not_delete_external_file(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_text('keep')
        self.report.parent.mkdir()
        self.report.symlink_to(sentinel)
        self.assertEqual(self.call('run').returncode, 1)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_detached_ensure_starts_once_and_finishes(self):
        self.assertEqual(self.call('ensure', TEST_DELAY='.4').returncode, 1)
        self.assertEqual(self.call('ensure', TEST_DELAY='.4').returncode, 1)
        deadline = time.time() + 10
        while time.time() < deadline and self.receipt()['status'] == 'running':
            time.sleep(.05)
        self.assertEqual(self.receipt()['status'], 'passed')
        self.assertEqual((self.home / 'state/count').read_text(), '1')

    def test_uncommitted_fix_cannot_qualify_published_head(self):
        (self.copy/'source.py').write_text('def simple():\n    return 7\n')
        self.assertEqual(self.call('run').returncode, 1)
        self.assertIn('Commit source and test changes', self.receipt()['reason'])
        self.assertEqual(self.done().returncode, 1)

    def test_poll_waits_for_new_ready_event_after_agent_edits(self):
        status = self.home/'state/task.status'
        status.write_text('done: ready\n')
        def scan():
            subprocess.run([str(REPO/'bin/fm-quality-check.sh'), 'scan'], env=self.env, check=True, capture_output=True, timeout=30)
        def wait():
            deadline = time.time()+10
            while time.time()<deadline and self.receipt()['status']=='running':
                time.sleep(.05)
            self.assertEqual(self.receipt()['status'], 'passed')
        scan(); wait()
        original = self.receipt()['key']
        (self.copy/'source.py').write_text('def simple():\n    return 8\n')
        scan()
        self.assertEqual(self.receipt()['key'], original)
        self.assertFalse((self.home/'state/feedback').exists())
        self.assertEqual(self.done().returncode, 1)
        self.git(self.copy, 'commit', '-qam', 'repair')
        with status.open('a') as output:
            output.write('done: ready\n')
        scan(); wait()
        self.assertNotEqual(self.receipt()['key'], original)
        self.assertEqual((self.home/'state/count').read_text(), '2')

    def test_real_watcher_poll_starts_quality_check(self):
        (self.home/'state/task.status').write_text('done: ready\n')
        env = dict(self.env, FM_POLL='1', FM_HEARTBEAT='9999', FM_CHECK_INTERVAL='9999')
        watcher = subprocess.Popen([str(REPO/'bin/fm-watch.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if (self.home/'state/quality/task.json').exists() and self.receipt()['status'] == 'passed':
                    break
                if watcher.poll() is not None and not (self.home/'state/quality/task.json').exists():
                    break
                time.sleep(.1)
            self.assertTrue((self.home/'state/quality/task.json').exists(), 'watcher never started validation')
            self.assertEqual(self.receipt()['status'], 'passed')
        finally:
            if watcher.poll() is None:
                try:
                    os.killpg(watcher.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                watcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(watcher.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                watcher.communicate(timeout=10)

    def test_reused_pid_does_not_hold_or_kill_unrelated_process(self):
        self.assertEqual(self.call('run').returncode, 0)
        previous = self.receipt()
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            # Keep the original runner's actual identity while its numeric PID
            # now names another real process, as it would after PID reuse.
            previous.update(status='running', pid=unrelated.pid, deadline=time.time()+30)
            (self.home/'state/quality/task.json').write_text(json.dumps(previous))
            self.assertEqual(self.call('run').returncode, 0)
            self.assertEqual(self.receipt()['status'], 'passed')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_incarnation_change_invalidates_pass(self):
        self.assertEqual(self.call('run').returncode, 0)
        self.meta.write_text(self.meta.read_text().replace('s1.test', 's2.test'))
        self.assertEqual(self.call('status').returncode, 1)

    def test_publication_head_must_match_checked_copy(self):
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.call('status', FM_QUALITY_EXPECTED_HEAD=self.base).returncode, 1)

    def done(self, mode='local-only', note='done: ready'):
        return subprocess.run(['bash', '-c', '. "$1/bin/fm-dod-lib.sh"; fm_dod_accept_ship_done ship "$2" "$3" "$4" "$5" "$6" task "$7"', '_', str(REPO), mode, str(self.copy), str(self.project), note, str(self.home/'state'), str(self.meta)], env=self.env, capture_output=True, text=True, timeout=30)

    def test_readiness_and_prevalidation_handoff_require_receipt(self):
        self.assertEqual(self.done().returncode, 1)
        self.assertEqual(self.done('no-mistakes', 'done: implemented').returncode, 1)
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.done().returncode, 0)
        self.assertEqual(self.done('no-mistakes', 'done: implemented').returncode, 0)
        (self.copy/'source.py').write_text('def simple():\n    return 5\n')
        self.assertEqual(self.done().returncode, 1)

    def test_successful_quality_does_not_pollute_named_head_failure(self):
        self.assertEqual(self.call('run').returncode, 0)
        result = self.done('direct-PR', 'done: PR https://github.com/o/r/pull/1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIn('unreachable outside the worker copy', result.stdout)

    def test_pr_registration_cannot_skip_quality_when_forge_has_head(self):
        fakebin = self.home/'fakebin'
        fakebin.mkdir()
        gh = fakebin/'gh'
        gh.write_text('#!/bin/sh\ncase "$*" in *headRefOid*) printf "%s\\n" "$TEST_PR_HEAD";; *) printf "false\\n";; esac\n')
        gh.chmod(0o755)
        tmux = fakebin/'tmux'
        tmux.write_text('#!/bin/sh\nexit 1\n')
        tmux.chmod(0o755)
        head = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.meta.write_text(self.meta.read_text().replace('mode=local-only', 'mode=direct-PR') + 'window=fm-quality-fixture:fm-task\nendpoint_task_id=task\nharness=claude\n')
        env = dict(self.env, PATH=str(fakebin)+os.pathsep+self.env['PATH'], TEST_PR_HEAD=head)
        checked = subprocess.run([str(REPO/'bin/fm-pr-check.sh'), 'task', 'https://github.com/o/r/pull/1'], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 1, checked.stdout+checked.stderr)
        self.assertIn('quality:', checked.stderr)
        self.assertNotIn('pr=', self.meta.read_text())
        deadline = time.time()+10
        while time.time()<deadline and self.receipt()['status']=='running':
            time.sleep(.05)
        self.assertEqual(self.receipt()['status'], 'passed')

    def test_recorded_forge_head_cannot_bypass_quality(self):
        head = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.meta.write_text(self.meta.read_text() + 'pr=https://github.com/o/r/pull/1\npr_head=' + head + '\n')
        self.assertEqual(self.done('no-mistakes', 'done: PR https://github.com/o/r/pull/1 checks green').returncode, 1)
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.done('no-mistakes', 'done: PR https://github.com/o/r/pull/1 checks green').returncode, 0)

    def test_poll_scan_runs_without_an_agent_instruction(self):
        (self.home/'state/task.status').write_text('done: ready\n')
        result = subprocess.run([str(REPO/'bin/fm-quality-check.sh'), 'scan'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.time() + 10
        while time.time() < deadline and self.receipt()['status'] == 'running':
            time.sleep(.05)
        self.assertEqual(self.receipt()['status'], 'passed')
        self.assertEqual(self.done().returncode, 0)

    @unittest.skipUnless(REAL_PYTHON, 'set FM_TEST_CRAPPER_PYTHON to exercise the real pinned Crapper package')
    def test_removed_type_overload_does_not_score_untouched_implementation(self):
        self.rule['crapper_python'] = REAL_PYTHON
        self.write_config()
        signature = 'from typing import overload\n\n@overload\ndef choose(x: int) -> int: ...\n\n'
        implementation = 'def choose(x):\n' + ''.join(f'    if x == {i}:\n        x += 1\n' for i in range(8)) + '    return x\n'
        (self.project/'source.py').write_text(signature+implementation)
        self.git(self.project, 'commit', '-qam', 'typed baseline')
        base = self.git(self.project, 'rev-parse', 'HEAD').stdout.strip()
        old_copy = self.copy
        self.copy = self.root/'overload-copy'
        self.git(self.project, 'worktree', 'add', '-qb', 'overload-task', str(self.copy), 'main')
        (self.copy/'source.py').write_text(implementation)
        self.git(self.copy, 'commit', '-qam', 'remove type-only declaration')
        self.meta.write_text(self.meta.read_text().replace(str(old_copy), str(self.copy)).replace(self.base, base))
        result = self.call('run')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(json.loads(Path(self.receipt()['entries']).read_text()), [])

    @unittest.skipUnless(REAL_PYTHON, 'set FM_TEST_CRAPPER_PYTHON to exercise the real pinned Crapper package')
    def test_real_crapper_ignores_untouched_function_and_rejects_complex_edit(self):
        self.rule['crapper_python'] = REAL_PYTHON
        self.write_config()
        baseline = 'def simple():\n    return 1\n\ndef legacy(x):\n' + ''.join(f'    if x == {i}:\n        x += 1\n' for i in range(8)) + '    return x\n'
        (self.project / 'source.py').write_text(baseline)
        self.git(self.project, 'commit', '-qam', 'legacy baseline')
        new_base = self.git(self.project, 'rev-parse', 'HEAD').stdout.strip()
        old_copy = self.copy
        self.copy = self.root / 'real-copy'
        self.git(self.project, 'worktree', 'add', '-qb', 'real-task', str(self.copy), 'main')
        (self.copy / 'source.py').write_text(baseline.replace('return 1', 'return 2'))
        self.git(self.copy, 'commit', '-qam', 'simple change')
        self.meta.write_text(self.meta.read_text().replace(str(old_copy), str(self.copy)).replace(self.base, new_base))
        self.assertEqual(self.call('run').returncode, 0)
        entries = json.loads(Path(self.receipt()['entries']).read_text())
        self.assertEqual([entry['name'] for entry in entries], ['simple'])
        (self.copy / 'source.py').write_text(baseline.replace('x += 1', 'x += 2'))
        self.git(self.copy, 'commit', '-qam', 'change complex function')
        result = self.call('run')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        entries = json.loads(Path(self.receipt()['entries']).read_text())
        legacy = next(entry for entry in entries if entry['name'] == 'legacy')
        self.assertEqual(legacy['coverage'], 100.0)
        self.assertEqual(legacy['crap'], legacy['complexity'])
        self.assertGreater(legacy['crap'], 7)


if __name__ == '__main__':
    unittest.main()

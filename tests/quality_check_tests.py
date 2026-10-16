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
if os.environ.get('TEST_CRAPPER_ERROR'):
    print('fixture discovery output')
    print(os.environ['TEST_CRAPPER_ERROR'], file=sys.stderr)
    sys.exit(1)
entries=json.loads(os.environ['TEST_ENTRIES']) if os.environ.get('TEST_ENTRIES') else [{'name':'fixture','crap':float(os.environ.get('TEST_SCORE','1'))}]
Path(r['output']).write_text(json.dumps(entries))
print('fixture CRAP report')
sys.exit(2 if any(entry['crap']>7 for entry in entries) else 0)
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
for line in range(int(os.environ.get('TEST_NOISE','0'))):
    print(f'emulator noise {line}')
if os.environ.get('TEST_FAIL_OUTPUT'):
    print(os.environ['TEST_FAIL_OUTPUT'])
if os.environ.get('TEST_EDIT'):
    (root/'source.py').write_text('def simple():\\n    return 99\\n')
if os.environ.get('TEST_ARTIFACTS'):
    (root/f'.coverage.host.{os.getpid()}').write_text(str(time.time()))
    (root/'coverage.xml').write_text(str(time.time()))
    (root/'htmlcov').mkdir(exist_ok=True)
    (root/'htmlcov/index.html').write_text(str(time.time()))
if os.environ.get('TEST_NEW_INPUT'):
    (root/os.environ['TEST_NEW_INPUT']).write_text('def late():\\n    return 1\\n')
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
        self.assertIn('The quality check failed for commit', feedback)
        self.assertEqual(self.call('ensure').returncode, 1)
        self.assertEqual((self.home / 'state/count').read_text(), '1')
        self.assertEqual((self.home / 'state/feedback').read_text(), feedback)
        (self.copy / 'source.py').write_text('def simple():\n    return 3\n')
        self.git(self.copy, 'commit', '-qam', 'repair')
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)

    def feedback(self):
        message = (self.home / 'state/feedback').read_text().removeprefix('task\n').rstrip('\n')
        self.assertLess(len(message), 6000)
        self.assertTrue(message.endswith('Full log, only if you need more detail: ' + self.receipt()['log']), message)
        self.assertIn('Do not change the quality configuration or receipt to bypass it.', message)
        return message

    def test_score_failure_message_is_self_contained(self):
        entries = [{'name': 'startSession', 'path': 'src/store.ts', 'complexity': 11, 'coverage': 100.0, 'crap': 11.0},
                   {'name': 'growing', 'path': 'src/store.ts', 'complexity': 9, 'coverage': 50.0, 'crap': 19.125},
                   {'name': 'untested', 'path': 'src/other.ts', 'complexity': 4, 'coverage': 20.0, 'crap': 12.192},
                   {'name': 'summaryNumber', 'path': 'src/other.ts', 'complexity': 5, 'coverage': 85.7143, 'crap': 5.0729}]
        self.assertEqual(self.call('run', TEST_ENTRIES=json.dumps(entries), TEST_NOISE='40').returncode, 1)
        message = self.feedback()
        head = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.assertTrue(message.startswith(f'The quality check failed for commit {head[:8]}.'), message)
        self.assertNotIn(head, message)
        self.assertNotIn('emulator noise', message)
        self.assertIn('What passed: the tests (all pass) and the coverage run.', message)
        self.assertIn('What failed: 3 of the 4 changed functions score above the limit of 7.', message)
        self.assertIn('  startSession  (src/store.ts)\n    branches: 11   test coverage: 100%   score: 11.0   FAILS (limit 7)', message)
        self.assertIn('  growing  (same file)\n    branches: 9   test coverage: 50%   score: 19.1   FAILS (limit 7)', message)
        self.assertIn('  summaryNumber  (same file)\n    branches: 5   test coverage: 85.7%   score: 5.1   passes', message)
        self.assertIn('At 100% coverage the score equals the number of branches', message)
        self.assertIn('Write tests that check behavior, not tests that only run the lines.', message)
        self.assertIn('moving the same logic around without making it simpler does not help', message)
        self.assertIn('  startSession: coverage is already 100%, so only option 2 can work: get it to 7 branches or fewer.', message)
        self.assertIn('  growing: coverage is 50%, but even full coverage would leave the score at 9, so it needs option 2', message)
        self.assertIn('  untested: coverage is 20%, so better tests (option 1) can bring it under the limit', message)
        self.assertNotIn('  summaryNumber:', message)
        self.assertIn('do not refactor it yourself. Report it to Firstmate with the function name, its score, and what you changed in it, then wait', message)
        self.assertIn('Do not delete or weaken behavior, or revert changes, just to get under the limit.', message)
        self.assertIn('commit the repair and report ready again', message)
        self.assertIn('report that blocker to Firstmate', message)

    def test_score_failure_message_stays_bounded(self):
        entries = [{'name': f'function{index}' + 'x' * 200, 'path': f'src/{index}/' + 'y' * 300, 'complexity': 20, 'coverage': 10.0, 'crap': 311.6}
                   for index in range(40)]
        self.assertEqual(self.call('run', TEST_ENTRIES=json.dumps(entries)).returncode, 1)
        message = self.feedback()
        self.assertIn('What failed: 40 of the 40 changed functions score above the limit of 7.', message)
        self.assertIn('...and 34 more (see the full log).', message)
        self.assertIn('  For the rest: below 100% coverage tests can help; at 100% only simplifying can.', message)
        self.assertIn('Do not delete or weaken behavior', message)

    def test_test_failure_message_leads_with_failing_lines(self):
        failure = 'FAILED tests/test_store.py::test_start - AssertionError: expected 2'
        self.assertEqual(self.call('run', TEST_NOISE='200', TEST_FAIL_OUTPUT=failure, TEST_COVERAGE_EXIT='3').returncode, 1)
        message = self.feedback()
        self.assertTrue(message.startswith('The quality check failed for commit '), message)
        self.assertIn('the coverage command, which runs the tests, exited 3.', message)
        self.assertIn('Failing tests and error lines from its output:\n' + failure, message)
        self.assertIn('Last lines of its output:\n', message)
        self.assertIn('emulator noise 199', message)
        self.assertNotIn('emulator noise 0\n', message)
        self.assertEqual(message.count(failure), 1)
        self.assertNotIn('changed functions score', message)

    def test_setup_failure_message_leads_with_the_error_line(self):
        error = 'No coverage metrics for changed function simple in source.py; declare a fresh report that covers it'
        self.assertEqual(self.call('run', TEST_CRAPPER_ERROR=error, TEST_NOISE='5').returncode, 1)
        message = self.feedback()
        self.assertTrue(message.startswith('The quality check could not finish for commit '), message)
        self.assertEqual(message.splitlines()[0].split(': ', 1)[1], error)
        self.assertIn('Check result: Crapper exited 1\n', message)
        self.assertIn('What passed: the tests and the coverage run. Scoring did not finish.', message)
        self.assertNotIn('emulator noise', message)

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

    def test_secondary_coverage_artifacts_do_not_invalidate_source_identity(self):
        result = self.call('run', TEST_ARTIFACTS='1')
        self.assertEqual(result.returncode, 0, result.stdout + self.receipt()['reason'])
        self.assertEqual(self.call('status').returncode, 0)
        self.assertEqual(self.call('run', TEST_ARTIFACTS='1').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)

    def test_new_source_or_test_written_beside_artifacts_is_still_an_input(self):
        for name in ('late.py', 'test_late.py'):
            self.assertEqual(self.call('run', TEST_ARTIFACTS='1', TEST_NEW_INPUT=name).returncode, 1)
            self.assertEqual(self.receipt()['status'], 'failed')
            (self.copy/name).unlink()
        self.assertEqual(self.call('run', TEST_ARTIFACTS='1').returncode, 0)
        (self.copy/'test_new.py').write_text('def test_new():\n    assert True\n')
        self.assertEqual(self.call('status').returncode, 1)

    def test_missing_or_stale_declared_report_fails_despite_artifacts(self):
        self.report.parent.mkdir()
        self.report.write_text('stale coverage')
        self.assertEqual(self.call('run', TEST_ARTIFACTS='1', TEST_MISSING='1').returncode, 1)
        self.assertEqual(self.receipt()['status'], 'failed')
        self.assertFalse(self.report.exists())
        self.assertEqual(self.call('status').returncode, 1)

    def test_repeated_operational_meta_fields_use_last_value(self):
        with self.meta.open('a') as meta:
            meta.write('decisions_reviewed=1\ndecision_keys=a\ndecisions_reviewed=1\ndecision_keys=a,b\n')
        self.assertEqual(self.call('run').returncode, 0)
        self.assertEqual(self.call('status').returncode, 0)
        self.assertEqual(self.done().returncode, 0)
        with self.meta.open('a') as meta:
            meta.write('spawn_gen=s2.test\n')
        self.assertEqual(self.call('status').returncode, 1)

    def test_missing_owned_identity_field_is_rejected(self):
        self.meta.write_text(self.meta.read_text().replace(f'worktree={self.copy}\n', ''))
        result = self.call('run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('worktree', result.stdout)
        self.assertFalse((self.home / 'state/count').exists())

    def test_new_source_beside_report_invalidates_pass(self):
        self.rule['coverage_reports'] = ['backend/lcov.info']
        self.write_config()
        self.env['TEST_REPORT'] = 'backend/lcov.info'
        self.assertEqual(self.call('run').returncode, 0)
        (self.copy/'backend/new.py').write_text('def new():\n    return 1\n')
        self.assertEqual(self.call('status').returncode, 1)

    def test_untracked_inputs_in_output_named_directories_are_bound(self):
        self.assertEqual(self.call('run').returncode, 0)
        accepted_key = self.receipt()['key']
        for name in ('tests/coverage/test_new.py', 'cmd/build/new.go',
                     '.metrics/new.py', 'target/new.rs', 'dist/new.js',
                     'out/new.py', 'htmlcov/new.py', 'node_modules/new.js',
                     '.venv/new.py', '__pycache__/new.py', '.pytest_cache/new.py',
                     '.nyc_output/new.js', 'nested/coverage.xml'):
            with self.subTest(name=name):
                path = self.copy/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('new input\n')
                try:
                    self.assertEqual(self.call('status').returncode, 1)
                    result = self.call('run')
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn('Commit source and test changes', self.receipt()['reason'])
                    first_key = self.receipt()['key']
                    self.assertNotEqual(first_key, accepted_key)
                    path.write_text('edited input\n')
                    self.assertEqual(self.call('run').returncode, 1)
                    self.assertNotEqual(self.receipt()['key'], first_key)
                finally:
                    path.unlink()
                self.assertEqual(self.call('run').returncode, 0)

    def test_local_landing_uses_the_checked_commit_when_branch_advances(self):
        self.check_local_landing(advance=True)

    def test_local_landing_of_current_checked_commit(self):
        self.check_local_landing(advance=False)

    def check_local_landing(self, advance):
        self.assertEqual(self.call('run').returncode, 0)
        checked = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        (self.copy/'source.py').write_text('def simple():\n    return 3\n')
        self.git(self.copy, 'commit', '-qam', 'unchecked successor')
        successor = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.git(self.copy, 'reset', '--hard', checked)
        with self.meta.open('a') as meta:
            meta.write('branch=task\n')
        shutil.copy2(REPO/'bin/fm-merge-local.sh', self.home/'bin/fm-merge-local.sh')
        for library in (REPO/'bin').glob('*-lib.sh'):
            shutil.copy2(library, self.home/'bin'/library.name)
        hold = self.home/'bin/fm-captain-hold.sh'
        hold.write_text('#!/bin/sh\nset -e\n[ -z "$TEST_SUCCESSOR" ] || git -C "$TEST_TASK_COPY" merge --ff-only "$TEST_SUCCESSOR" >/dev/null\nexit 3\n')
        hold.chmod(0o755)
        result = subprocess.run([str(self.home/'bin/fm-merge-local.sh'), 'task'],
                                env=dict(self.env, TEST_TASK_COPY=str(self.copy), TEST_SUCCESSOR=successor if advance else ''),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip(), successor if advance else checked)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD').stdout.strip(), checked)

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

    def forgejo_check(self, published):
        fakebin = self.home/'fakebin'
        fakebin.mkdir(exist_ok=True)
        tea = fakebin/'tea'
        tea.write_text('#!/bin/sh\ncase "$1 $2" in "login list") printf \'[{"name":"fixture","url":"https://forgejo.example"}]\\n\';; *) printf \'{"state":"open","merged":false,"head":{"sha":"%s"}}\\n\' "$TEST_PR_HEAD";; esac\n')
        tea.chmod(0o755)
        tmux = fakebin/'tmux'
        tmux.write_text('#!/bin/sh\nexit 1\n')
        tmux.chmod(0o755)
        if 'mode=direct-PR' not in self.meta.read_text():
            self.meta.write_text(self.meta.read_text().replace('mode=local-only', 'mode=direct-PR') + 'window=fm-quality-fixture:fm-task\nendpoint_task_id=task\nharness=claude\n')
        env = dict(self.env, PATH=str(fakebin)+os.pathsep+self.env['PATH'], TEST_PR_HEAD=published)
        return subprocess.run([str(REPO/'bin/fm-pr-check.sh'), 'task', 'https://forgejo.example/o/r/pulls/1'], env=env, capture_output=True, text=True, timeout=30)

    def test_forgejo_registration_verifies_receipt_against_live_head(self):
        head = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(self.call('run').returncode, 0)
        stale = self.forgejo_check(self.base)
        self.assertEqual(stale.returncode, 1)
        self.assertIn('published head differs', stale.stderr)
        current = self.forgejo_check(head)
        self.assertNotIn('quality:', current.stderr, current.stdout + current.stderr)

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
    def test_real_lcov_requires_metrics_in_the_changed_function(self):
        self.check_real_function_metrics('module.py', 'def stable():\n    return 1\n',
                                         'def stable():\n    return 1\n\ndef changed():\n    return 2\n',
                                         'lcov.info', 'changed', [
            ('SF:module.py\nDA:2,1\nend_of_record\n', None),
            ('SF:module.py\nBRDA:2,0,0,1\nend_of_record\n', None),
            ('SF:module.py\nDA:5,0\nend_of_record\n', 0),
            ('SF:module.py\nBRDA:5,0,0,-\nend_of_record\n', 0),
            ('SF:module.py\nDA:5,1\nend_of_record\n', 100),
        ])

    @unittest.skipUnless(REAL_PYTHON, 'set FM_TEST_CRAPPER_PYTHON to exercise the real pinned Crapper package')
    def test_real_go_requires_statements_in_the_changed_function(self):
        stable = 'package sample\n\nfunc Stable() int {\n    return 1\n}\n'
        self.check_real_function_metrics('module.go', stable,
                                         stable + '\nfunc Changed() int {\n    return 2\n}\n',
                                         'coverage.out', 'Changed', [
            ('mode: set\nexample/module.go:3.1,5.2 1 1\n', None),
            ('mode: set\nexample/module.go:7.1,9.2 0 1\n', None),
            ('mode: set\nexample/module.go:7.1,9.2 1 0\n', 0),
            ('mode: set\nexample/module.go:7.1,9.2 1 1\n', 100),
        ])

    @unittest.skipUnless(REAL_PYTHON, 'set FM_TEST_CRAPPER_PYTHON to exercise the real pinned Crapper package')
    def test_real_jacoco_requires_instruction_metrics_for_the_changed_method(self):
        cases = []
        for line, counter, percent in [(3, '', None), (3, 'missed="2"', None), (3, 'covered="2"', None),
                                       (3, 'missed="" covered="2"', None), (3, 'missed="2" covered=""', None),
                                       (3, 'missed="0" covered="0"', None),
                                       (2, 'missed="0" covered="2"', None),
                                       (3, 'missed="2" covered="0"', 0),
                                       (3, 'missed="0" covered="2"', 100)]:
            cases.append((f'<report><package name=""><class name="Metrics"><method name="changed" line="{line}"><counter type="INSTRUCTION" {counter}/></method></class></package></report>', percent))
        self.check_real_function_metrics('Metrics.java', 'class Metrics {\n    int stable() { return 1; }\n}\n',
                                         'class Metrics {\n    int stable() { return 1; }\n    int changed() { return 2; }\n}\n',
                                         'jacoco.xml', 'changed', cases)

    def check_real_function_metrics(self, filename, before, after, report, function, cases):
        self.git(self.copy, 'reset', '--hard', self.base)
        (self.copy/filename).write_text(before)
        self.git(self.copy, 'add', filename)
        self.git(self.copy, 'commit', '-qm', 'function baseline')
        baseline = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.meta.write_text(self.meta.read_text().replace(self.base, baseline))
        (self.copy/filename).write_text(after)
        self.git(self.copy, 'commit', '-qam', 'new function')
        self.coverage.write_text('import os\nfrom pathlib import Path\np=Path(os.environ["TEST_REPORT"]); p.parent.mkdir(exist_ok=True); p.write_text(os.environ["TEST_METRICS"])\n')
        self.rule['coverage_reports'] = ['coverage/' + report]
        self.rule['crapper_python'] = REAL_PYTHON
        self.write_config()
        for content, percent in cases:
            with self.subTest(report=content):
                result = self.call('run', TEST_REPORT='coverage/' + report, TEST_METRICS=content)
                log = Path(self.receipt()['log']).read_text()
                if percent is None:
                    self.assertEqual(result.returncode, 1, result.stdout + log)
                    self.assertRegex(log, f'No coverage metrics for changed function {function}|Missing JaCoCo instruction metrics')
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + log)
                    entries = json.loads(Path(self.receipt()['entries']).read_text())
                    self.assertEqual([(entry['name'], entry['coverage']) for entry in entries], [(function, percent)])

    def crew_completion(self, primary, ledger='', selected='', outcome='passed', head=None, full=True):
        checked = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        run_head = head or checked
        run = f'run:\n  id: 01TASK\n  branch: task\n  status: completed\n  head: {run_head[:8]}\n'
        if full:
            run += f'  head_sha: {run_head}\n'
        if outcome:
            run += f'outcome: {outcome}\n'
        overview = f'count: 1 of 1 total\nruns[1]{{id,branch,status,head,pr}}:\n  01TASK,task,completed,{run_head[:8]},\n' if selected else run
        fakebin = self.home/'fakebin'
        fakebin.mkdir(exist_ok=True)
        driver = fakebin/'no-mistakes'
        driver.write_text('#!/bin/sh\ncase "$*" in\n"axi status") printf "%s\\n" "$TEST_AXI_STATUS";;\n"axi status --run 01TASK") printf "%s\\n" "$TEST_AXI_SELECTED";;\naxi) printf "%s\\n" "$TEST_AXI_HOME";;\nruns*) printf "%s\\n" "$TEST_RUNS";;\n"daemon status") printf "daemon running\\n";;\nesac\n')
        driver.chmod(0o755)
        return subprocess.run([str(REPO/'bin/fm-crew-state.sh'), 'task'],
                              env=dict(self.env, PATH=str(fakebin) + os.pathsep + os.environ['PATH'],
                                       TEST_AXI_STATUS=primary or run, TEST_AXI_HOME=overview,
                                       TEST_AXI_SELECTED=run, TEST_RUNS=ledger),
                              capture_output=True, text=True, timeout=30)

    def prepare_crew_completion(self):
        with self.meta.open('a') as meta:
            meta.write('mode=no-mistakes\nwindow=fm:fm-task\nharness=claude\n')
        self.assertEqual(self.call('run').returncode, 0)

    def test_crew_completion_uses_full_sha_for_detailed_run_outcomes(self):
        self.prepare_crew_completion()
        for selected in (False, True):
            for outcome in ('passed', 'passed-with-override', 'passed-with-skips', 'checks-passed', ''):
                with self.subTest(selected=selected, outcome=outcome):
                    result = self.crew_completion('', selected=selected, outcome=outcome)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('state: done', result.stdout)
                    self.assertIn('source: run-step', result.stdout)
        result = self.crew_completion('', full=False)
        self.assertIn('state: done', result.stdout)

    def test_crew_completion_uses_the_attributed_ledger_head(self):
        self.prepare_crew_completion()
        checked = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        ledger = f'completed task {checked[:8]} 2026-10-15 10:00'
        for branch in ('other', 'task'):
            with self.subTest(branch=branch):
                primary = f'run:\n  branch: {branch}\n  status: completed\n  head: {self.base}\n  head_sha: {self.base}\noutcome: passed\n'
                result = self.crew_completion(primary, ledger=ledger)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('state: done', result.stdout)
                self.assertIn('source: run-step', result.stdout)
        result = self.crew_completion(primary, ledger=ledger.replace('completed', 'running'))
        self.assertIn('state: working', result.stdout)

    def test_crew_completion_rejects_a_run_at_a_different_commit(self):
        self.prepare_crew_completion()
        checked = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.git(self.copy, 'commit', '--allow-empty', '-qm', 'later validation head')
        successor = self.git(self.copy, 'rev-parse', 'HEAD').stdout.strip()
        self.git(self.copy, 'reset', '--hard', checked)
        primary = f'run:\n  branch: other\n  status: completed\n  head: {checked}\n  head_sha: {checked}\noutcome: passed\n'
        ledger = f'completed task {successor[:8]} 2026-10-15 10:00'
        same_branch = primary.replace('branch: other', 'branch: task')
        canonical = same_branch.replace(f'head_sha: {checked}', f'head_sha: {successor}')
        for result in (self.crew_completion('', head=successor), self.crew_completion(primary, ledger=ledger),
                       self.crew_completion(same_branch, ledger=ledger), self.crew_completion(canonical)):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('state: blocked', result.stdout)
            self.assertIn('source: quality', result.stdout)

    @unittest.skipUnless(REAL_PYTHON, 'set FM_TEST_CRAPPER_PYTHON to exercise the real pinned Crapper package')
    def test_real_crapper_rejects_changed_function_absent_from_reports(self):
        self.rule['crapper_python'] = REAL_PYTHON
        self.write_config()
        (self.copy/'backend.py').write_text('def untested():\n    return 1\n')
        self.git(self.copy, 'add', 'backend.py')
        self.git(self.copy, 'commit', '-qm', 'uncovered backend')
        result = self.call('run')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('Crapper exited 1', self.receipt()['reason'])
        self.assertIn('No coverage metrics for changed function untested in backend.py', Path(self.receipt()['log']).read_text())
        self.assertIn(': No coverage metrics for changed function untested in backend.py', self.feedback().splitlines()[0])
        self.assertEqual(self.call('status').returncode, 1)

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
        message = self.feedback()
        self.assertIn(f"  legacy  ({legacy['path']})\n    branches: {legacy['complexity']}   test coverage: 100%   score: {legacy['crap']:.1f}   FAILS (limit 7)", message)
        self.assertIn('  legacy: coverage is already 100%, so only option 2 can work: get it to 7 branches or fewer.', message)


if __name__ == '__main__':
    unittest.main()

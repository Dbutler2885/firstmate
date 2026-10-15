#!/usr/bin/env python3
"""Implementation of fm-quality-check.sh; its header owns the operator contract."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

CODE = Path(__file__).resolve().parent
HOME = Path(os.environ.get('FM_HOME') or os.environ.get('FM_ROOT_OVERRIDE') or CODE.parent)
STATE = Path(os.environ.get('FM_STATE_OVERRIDE') or HOME / 'state')
CONFIG = Path(os.environ.get('FM_CONFIG_OVERRIDE') or HOME / 'config') / 'project-quality.json'
ACTION, TASK = sys.argv[1:3]
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', TASK):
    sys.exit('Invalid quality task id')
META = Path(os.environ.get('FM_QUALITY_META_OVERRIDE') or STATE / f'{TASK}.meta') if ACTION == 'status' else STATE / f'{TASK}.meta'
STORE = STATE / 'quality'
RESULT = STORE / f'{TASK}.json'


def ordinary(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f'Not an ordinary single-link file: {path}')


def atomic(path, value):
    fd, name = tempfile.mkstemp(prefix='.quality-', dir=STORE)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def git(root, *args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(root), *args], stderr=subprocess.PIPE)


def metadata():
    ordinary(META)
    result = {}
    for line in META.read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator:
            result[key] = value
    for key in ('project', 'worktree'):
        if not result.get(key) or not Path(result[key]).is_absolute():
            raise ValueError(f'Missing or invalid task field: {key}')
    return result


def project_rule(project):
    ordinary(CONFIG)
    config = json.loads(CONFIG.read_text())
    if type(config.get('version')) is not int or config['version'] != 1 or not isinstance(config.get('projects'), dict):
        raise ValueError('Invalid project-quality.json: expected version 1 and projects object')
    rule = config['projects'].get(project.name)
    if rule is not None and not isinstance(rule, dict):
        raise ValueError(f'Invalid quality rule for {project.name}')
    return rule


def validate_rule(rule):
    threshold = rule.get('threshold', 7)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError('Quality threshold must be a finite nonnegative number')
    timeout = rule.get('timeout_seconds', 600)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('Quality timeout_seconds must be an integer from 1 to 3600')
    command = rule.get('coverage_command')
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in command):
        raise ValueError('coverage_command must be a nonempty argv array')
    interpreter = Path(rule.get('crapper_python', ''))
    if not interpreter.is_absolute() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError('crapper_python must name an installed executable interpreter')
    return threshold, timeout


def report_path(root, item):
    if isinstance(item, str):
        item = {'path': item, 'source_root': '.'}
    if not isinstance(item, dict):
        raise ValueError('Invalid coverage report')
    name = item.get('path', '')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or path.name not in {'lcov.info', 'jacoco.xml', 'coverage.out'} or len(path.parts) < 2:
        raise ValueError(f'Invalid coverage report path: {name}')
    if (root / path).is_symlink() or not (root / path).resolve().is_relative_to(root):
        raise ValueError(f'Coverage report escapes task copy: {name}')
    if git(root, 'ls-files', '--', name).strip():
        raise ValueError(f'Coverage reports must be generated, not tracked: {name}')
    source = (root / item.get('source_root', '.')).resolve()
    if not source.is_relative_to(root):
        raise ValueError('Coverage source_root escapes task copy')
    return {'path': name, 'source_root': str(source.relative_to(root))}


def optional_git(root, *args):
    try:
        return git(root, *args).decode().strip()
    except subprocess.CalledProcessError:
        return ''


def ancestor(root, before, after):
    return subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', before, after], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def task_base(root, project, meta):
    base = meta.get('quality_base', '')
    if not meta.get('spawn_gen') or not re.fullmatch(r'[0-9a-f]{40,64}', base):
        raise ValueError('Missing quality_base or task incarnation: start a new task before quality validation')
    git(root, 'rev-parse', '--verify', base + '^{commit}')
    target = meta.get('base_branch') or optional_git(project, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD').removeprefix('origin/') or optional_git(project, 'symbolic-ref', '--short', 'HEAD')
    best = base
    if target:
        for repository in (project, root):
            for reference in ('refs/remotes/origin/' + target, 'refs/heads/' + target):
                target_head = optional_git(repository, 'rev-parse', '--verify', reference + '^{commit}')
                merged = optional_git(root, 'merge-base', 'HEAD', target_head) if target_head else ''
                if merged and (not ancestor(root, best, 'HEAD') or ancestor(root, best, merged)):
                    best = merged
    return best


def excluded_output(name, reports):
    path = Path(name)
    if name in {'.coverage', 'coverage.xml', 'htmlcov/index.html'} or (len(path.parts) == 1 and path.name.startswith('.coverage.')):
        return True
    if path.parent.name == '__pycache__' and path.suffix == '.pyc':
        return True
    # A report written directly into a package must not hide new source files
    # beside it. Only the declared file, never its entire parent, is excluded.
    return any(name == report['path'] for report in reports)


def code_identity(root, meta, base, head, rule, reports):
    digest = hashlib.sha256(json.dumps([str(root), meta['spawn_gen'], base, head, rule], sort_keys=True).encode())
    tracked_changes = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD')
    digest.update(tracked_changes)
    digest.update((CODE / 'fm-quality-evaluate.py').read_bytes())
    digest.update((CODE / 'fm-quality-check.py').read_bytes())
    names = git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')
    untracked = sorted(name for name in names if name and not excluded_output(name, reports))
    for name in untracked:
        path = root / name
        digest.update(name.encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        else:
            with path.open('rb') as stream:
                digest.update(hashlib.file_digest(stream, 'sha256').digest())
    files = set(filter(None, git(root, 'diff', '--name-only', '-z', '--diff-filter=ACMR', base).decode().split('\0')))
    files.update(untracked)
    return digest.hexdigest(), sorted(files), not tracked_changes and not untracked


def check_expected_head(head):
    if ACTION != 'status':
        return
    expected = os.environ.get('FM_QUALITY_EXPECTED_HEAD', '')
    if os.environ.get('FM_QUALITY_REQUIRE_HEAD') == '1' and not expected:
        raise ValueError('published or validation-run head could not be verified')
    if expected and expected != head:
        raise ValueError('published head differs from the local copy; synchronize before quality validation')


def context():
    meta = metadata()
    if meta.get('kind', 'ship') != 'ship':
        return None
    project = Path(meta['project']).resolve()
    rule = project_rule(project)
    if rule is None:
        return None
    root = Path(meta['worktree']).resolve()
    top = git(root, 'rev-parse', '--show-toplevel').decode().strip()
    if os.path.samefile(root, project) or not os.path.samefile(top, root):
        raise ValueError('Quality checks require the isolated task copy')
    threshold, timeout = validate_rule(rule)
    declared = rule.get('coverage_reports')
    if not isinstance(declared, list) or not declared:
        raise ValueError('coverage_reports must declare the fresh coverage files')
    reports = [report_path(root, item) for item in declared]
    base = task_base(root, project, meta)
    head = git(root, 'rev-parse', 'HEAD').decode().strip()
    check_expected_head(head)
    key, files, clean = code_identity(root, meta, base, head, rule, reports)
    policy = hashlib.sha256(json.dumps([base, rule], sort_keys=True).encode() + (CODE / 'fm-quality-check.py').read_bytes() + (CODE / 'fm-quality-evaluate.py').read_bytes()).hexdigest()
    return {'key': key, 'policy': policy, 'clean': clean, 'root': str(root), 'base': base, 'head': head, 'incarnation': meta['spawn_gen'], 'rule': rule, 'reports': reports, 'files': files, 'threshold': threshold, 'timeout': timeout}


def read_result():
    if not RESULT.exists():
        return {}
    ordinary(RESULT)
    return json.loads(RESULT.read_text())


def process_identity(pid):
    source_root = Path(os.environ.get('FM_ROOT_OVERRIDE') or CODE.parent)
    probe = subprocess.run(['bash', '-c', '. "$1"; fm_pid_identity "$2"', '_', str(source_root/'bin/fm-wake-lib.sh'), str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    return probe.stdout.strip() if probe.returncode == 0 else ''


def live(result):
    if result.get('status') != 'running':
        return False
    pid = result.get('pid')
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid quality runner PID')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    identity = process_identity(pid)
    if not identity or not result.get('pid_identity'):
        return None
    return identity == result['pid_identity']


def timed_command(seconds, command):
    return ['bash', '-c', '. "$1"; shift; fm_exec_timed "$@"', '_', str(CODE/'fm-timeout-lib.sh'), str(seconds), '5', *command]


def explain(result):
    message = result.get('reason', 'quality check has not run')
    if result.get('log'):
        message += f"; report: {result['log']}"
    print('quality: ' + message)
    return 0 if result.get('status') == 'passed' else 1


def execute(ctx, result):
    ok = False
    reason = 'quality check interrupted'
    try:
        if not ctx['clean']:
            raise ValueError('Commit source and test changes before reporting ready for quality validation')
        for report in ctx['reports']:
            path = Path(ctx['root']) / report['path']
            if path.exists() or path.is_symlink():
                ordinary(path)
                path.unlink()
        with open(result['log'], 'a') as log:
            log.write(f"Task {TASK}; HEAD {ctx['head']}; base {ctx['base']}; threshold {ctx['threshold']}\n")
            log.flush()
            rc = subprocess.call(timed_command(ctx['timeout'], ctx['rule']['coverage_command']), cwd=ctx['root'], stdout=log, stderr=log)
            if rc:
                raise ValueError(f'coverage/tests exited {rc}')
            for report in ctx['reports']:
                path = Path(ctx['root']) / report['path']
                ordinary(path)
                if path.stat().st_size == 0:
                    raise ValueError(f'Empty coverage report: {path}')
            current = context()
            if current is None or current['key'] != ctx['key']:
                raise ValueError('code or configuration changed while tests ran')
            request = {key: ctx[key] for key in ('root', 'base', 'files', 'reports', 'threshold')}
            request['output'] = result['entries']
            request_path = Path(result['log'] + '.request.json')
            atomic(request_path, request)
            rc = subprocess.call(timed_command(ctx['timeout'], [ctx['rule']['crapper_python'], str(CODE / 'fm-quality-evaluate.py'), str(request_path)]), cwd=ctx['root'], stdout=log, stderr=log)
            if rc:
                raise ValueError(f'Crapper exited {rc} (2 means threshold exceeded)')
            entries_path = Path(result['entries'])
            ordinary(entries_path)
            entries = json.loads(entries_path.read_text())
            if not isinstance(entries, list):
                raise ValueError('Crapper did not produce a function-entry array')
            for entry in entries:
                score = entry.get('crap') if isinstance(entry, dict) else None
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
                    raise ValueError('Crapper produced an invalid or missing CRAP score')
                if score > ctx['threshold']:
                    raise ValueError('Crapper report exceeds the configured threshold')
            current = context()
            if current is None or current['key'] != ctx['key']:
                raise ValueError('code or configuration changed while Crapper ran')
            ok, reason = True, 'passed for the current code'
    except Exception as error:
        reason = str(error).replace('\n', ' ')[:600]
    result.update(status='passed' if ok else 'failed', reason=reason, finished=time.time())
    # A task that was removed or replaced while the command ran must not receive
    # a late receipt or steer from the retired incarnation.
    try:
        current = context()
        if current is None or current['incarnation'] != ctx['incarnation']:
            return 1
        if current['key'] != ctx['key']:
            ok = False
            result.update(status='failed', reason='code or task identity changed during quality validation')
        with locked():
            if read_result().get('key') != ctx['key']:
                return 1
            atomic(RESULT, result)
        (STATE / f'{TASK}.turn-ended').touch()
        if not ok and current is not None and current['key'] == ctx['key']:
            reports = result['log'] + (" and " + result['entries'] if Path(result['entries']).exists() else "")
            message = f"The mandatory quality check for HEAD {ctx['head']} failed: {reason}. Read {reports}, fix the failing tests or simplify/test the changed functions, commit the repair, and report ready again. If the report identifies a missing tool or coverage setup problem, report that blocker to Firstmate. Do not change the quality configuration or receipt to bypass it."
            notification = subprocess.run(timed_command(60, [str(CODE / 'fm-send.sh'), TASK, message]), cwd=HOME, env=dict(os.environ, FM_SEND_EXPECTED_SPAWN_GEN=ctx['incarnation'], FM_SEND_IDEMPOTENT='1'), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with open(result['log'], 'a') as log:
                log.write('\nFeedback delivery:\n' + notification.stdout)
                if notification.returncode:
                    log.write('Feedback delivery failed; Firstmate must inspect this report.\n')
    except Exception:
        # The published receipt is already a failure if identity changed. Never
        # reconstruct a missing task or write into a replaced task's inbox.
        return 1
    return 0 if ok else 1


class locked:
    def __enter__(self):
        STORE.mkdir(mode=0o700, exist_ok=True)
        if STORE.is_symlink() or not STORE.is_dir():
            raise ValueError('Unsafe quality state directory')
        fd = os.open(STORE / f'{TASK}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, 'a')
        fcntl.flock(fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        self.stream.close()


def main():
    if not CONFIG.exists() and not CONFIG.is_symlink():
        return 0
    ctx = context()
    if ctx is None:
        return 0
    if ACTION == 'status':
        result = read_result()
        if result.get('key') != ctx['key']:
            print('quality: current code has not passed validation')
            return 1
        if result.get('status') == 'running':
            running = live(result)
            if running is None:
                print('quality: runner process identity could not be verified')
                return 1
            if not running:
                print('quality: validation runner stopped without a result')
                return 1
        return explain(result)
    if ACTION == 'worker':
        with locked():
            result = read_result()
            if result.get('key') != ctx['key'] or ctx['key'] != sys.argv[3] or result.get('status') != 'running' or result.get('pid') != os.getpid():
                return 1
        return execute(ctx, result)
    if ACTION not in {'ensure', 'run'}:
        raise ValueError('Expected ensure, status, or run')
    token = ''
    if os.environ.get('FM_QUALITY_FROM_POLL') == '1':
        status = STATE / f'{TASK}.status'
        ordinary(status)
        token = hashlib.sha256(status.read_bytes()).hexdigest()
    with locked():
        previous = read_result()
        running = live(previous)
        if running is None:
            print('quality: runner process identity could not be verified')
            return 1
        if running:
            if time.time() > previous['deadline']:
                print('quality: previous runner exceeded its deadline; inspect before retrying')
                return 1
            return explain(previous)
        if previous.get('key') == ctx['key'] and ACTION == 'ensure' and previous.get('status') in {'passed', 'failed'}:
            return explain(previous)
        if ACTION == 'ensure' and token and token == previous.get('completion_token') and ctx['policy'] == previous.get('policy'):
            print('quality: code changed; waiting for a new ready report')
            return 1
        fd, filename = tempfile.mkstemp(prefix=f'{TASK}-', suffix='.log', dir=STORE)
        result = {'key': ctx['key'], 'policy': ctx['policy'], 'completion_token': token or previous.get('completion_token', ''), 'head': ctx['head'], 'status': 'running', 'reason': 'validation is running', 'pid': os.getpid(), 'deadline': time.time() + 2 * ctx['timeout'] + 30, 'log': filename, 'entries': filename + '.entries.json'}
        if ACTION == 'ensure':
            with os.fdopen(fd, 'w') as log:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'worker', TASK, ctx['key']], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            result['pid'] = child.pid
            result['pid_identity'] = process_identity(child.pid)
            if not result['pid_identity']:
                child.terminate()
                child.wait(timeout=5)
                raise ValueError('Could not capture quality runner process identity')
        else:
            os.close(fd)
            result['pid_identity'] = process_identity(os.getpid())
            if not result['pid_identity']:
                raise ValueError('Could not capture quality runner process identity')
        atomic(RESULT, result)
    if ACTION == 'run':
        return execute(ctx, result)
    return explain(result)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('quality: cannot validate: ' + str(error).replace('\n', ' '), file=sys.stdout)
        sys.exit(1)

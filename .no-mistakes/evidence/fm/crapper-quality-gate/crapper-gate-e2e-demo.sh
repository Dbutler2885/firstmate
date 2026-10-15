#!/usr/bin/env bash
# End-to-end demo of the Firstmate Crapper completion gate with real coverage.py and real Crapper.
set -u
REPO=$1; VENV=/tmp/crapper-UAAy/venv
W=$(mktemp -d /tmp/fm-quality-demo-XXXX); H=$W/home; P=$W/project; C=$W/copy
mkdir -p $H/state $H/config $H/bin
cp "$REPO"/bin/fm-quality-check.sh "$REPO"/bin/fm-quality-check.py "$REPO"/bin/fm-quality-evaluate.py "$REPO"/bin/fm-timeout-lib.sh "$REPO"/bin/fm-classify-lib.sh $H/bin/ 2>/dev/null
printf '#!/bin/sh\nprintf "[fm-send to %%s] %%s\\n" "$1" "$2" >> "$FM_HOME/state/feedback.log"\n' > $H/bin/fm-send.sh; chmod +x $H/bin/fm-send.sh
g(){ git -C "$@"; }
mkdir -p $P; g $P init -q -b main; g $P config user.email d@e.invalid; g $P config user.name demo
printf 'def greet(name):\n    return "hi " + name\n' > $P/app.py
printf 'import unittest\nfrom app import *\nclass T(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet("a"), "hi a")\n' > $P/test_app.py
printf 'coverage/\n.coverage\n' > $P/.gitignore
g $P add -A; g $P commit -qm base; BASE=$(g $P rev-parse HEAD)
g $P worktree add -qb task $C
cat > $H/config/project-quality.json <<J
{"version":1,"projects":{"project":{"crapper_python":"$VENV/bin/python","coverage_command":["sh","-c","$VENV/bin/python -m coverage run -m unittest -q 2>&1 && $VENV/bin/python -m coverage lcov -q -o coverage/lcov.info"],"coverage_reports":["coverage/lcov.info"],"threshold":7,"timeout_seconds":120}}}
J
printf 'kind=ship\nmode=local-only\nproject=%s\nworktree=%s\nquality_base=%s\nspawn_gen=s1.demo\n' $P $C $BASE > $H/state/task.meta
export FM_HOME=$H FM_ROOT_OVERRIDE=$REPO
Q=$H/bin/fm-quality-check.sh
step(){ echo; echo "### $*"; }
step "Config: config/project-quality.json"; cat $H/config/project-quality.json
step "Agent adds an untested branchy function classify() and commits"
cat >> $C/app.py <<'PY'

def classify(n):
    if n < 0:
        return "neg"
    if n == 0:
        return "zero"
    if n < 10:
        return "small"
    if n < 100:
        return "medium"
    return "large"
PY
g $C commit -qam "add classify"
step "Agent declares done; watcher poll runs 'fm-quality-check.sh scan' (no model involved)"
echo 'done: ready' > $H/state/task.status
$Q scan; for i in $(seq 1 200); do s=$(python3 -c "import json;print(json.load(open('$H/state/quality/task.json'))['status'])" 2>/dev/null); [ "$s" != running ] && [ -n "$s" ] && break; sleep .2; done
step "Receipt after poll"; python3 -c "import json;r=json.load(open('$H/state/quality/task.json'));print({k:r[k] for k in ('status','reason','head')})"
step "Read-only status gate (exit code used by done/PR/merge paths)"; $Q status task; echo "exit=$?"
step "Crapper report the agent is pointed at"; cat "$(python3 -c "import json;print(json.load(open('$H/state/quality/task.json'))['log'])")"
step "Feedback delivered to the agent's steering inbox"; cat $H/state/feedback.log
step "Agent repairs: adds tests covering every branch, commits, re-declares done"
cat >> $C/test_app.py <<'PY'
    def test_classify(self):
        for n, want in [(-1,"neg"),(0,"zero"),(5,"small"),(50,"medium"),(500,"large")]:
            self.assertEqual(classify(n), want)
PY
g $C commit -qam "test classify"
echo 'done: fixed CRAP' >> $H/state/task.status
$Q scan; for i in $(seq 1 200); do s=$(python3 -c "import json;print(json.load(open('$H/state/quality/task.json'))['status'])"); [ "$s" != running ] && break; sleep .2; done
step "Receipt after repair"; python3 -c "import json;r=json.load(open('$H/state/quality/task.json'));print({k:r[k] for k in ('status','reason','head')});print('scored:',json.load(open(r['entries'])))"
$Q status task; echo "status exit=$?"
step "Any subsequent edit invalidates the pass"
echo '# tweak' >> $C/app.py
$Q status task; echo "status exit=$?"
rm -rf $W

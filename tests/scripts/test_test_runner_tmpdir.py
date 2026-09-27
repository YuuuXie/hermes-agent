"""The real canonical shell entrypoint must preserve only allowed locations."""
import json
import os
import subprocess
import shutil
import sys
from pathlib import Path

import pytest


@pytest.mark.platforms("linux")
@pytest.mark.parametrize('activated', [False, True])
def test_canonical_runner_forwards_private_tmpdir_without_extra_environment(tmp_path, activated):
    source_root = Path(__file__).resolve().parents[2]
    # Execute an unchanged copy of the real shell entrypoint in a disposable
    # layout. Exercise both an explicit test Python and a current activation;
    # using the real checkout would accidentally launch the entire test suite.
    root = tmp_path / 'runner-root'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(source_root / 'scripts/run_tests.sh', root / 'scripts/run_tests.sh')
    shutil.copy2(source_root / 'scripts/_activation.sh', root / 'scripts/_activation.sh')
    home = tmp_path / 'home'
    home.mkdir()
    scratch = tmp_path / 'private scratch'
    scratch.mkdir()
    interpreter = tmp_path / 'python-probe'
    interpreter.write_text(
        '#!' + sys.executable + '\n'
        'import json,os,sys\n'
        'if any(a.endswith("run_tests_parallel.py") for a in sys.argv):\n'
        ' print(json.dumps({"tmpdir":os.getenv("TMPDIR"),"extra":os.getenv("TEST_NOT_ALLOWED")}))\n'
    )
    interpreter.chmod(0o700)
    env = {'PATH': os.environ['PATH'], 'HOME': str(home),
           'TMPDIR': str(scratch), 'TEST_NOT_ALLOWED': 'must-not-forward'}
    if activated:
        state = root / 'activation-state'
        (state / 'inputs/scripts').mkdir(parents=True)
        sentinel = state / 'installed'
        sentinel.touch()
        (state / 'inputs/.project-root').write_text(str(root.resolve()))
        shutil.copy2(root / 'scripts/run_tests.sh', state / 'inputs/scripts/run_tests.sh')
        (state / 'inputs/.test-environment').touch()
        env.update({'__HERMES_ACTIVATED': str(sentinel),
                    '__HERMES_TEST_PYTHON': str(interpreter)})
    else:
        env['HERMES_PYTHON'] = str(interpreter)
    result = subprocess.run(['bash', str(root/'scripts/run_tests.sh')],
        env=env,
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {'tmpdir':str(scratch), 'extra':None}

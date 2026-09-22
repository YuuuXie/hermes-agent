"""The real canonical shell entrypoint must preserve only allowed locations."""
import json
import os
import subprocess
import shutil
import sys
from pathlib import Path

import pytest


@pytest.mark.linux_only
@pytest.mark.parametrize('local_venv', [False, True])
def test_canonical_runner_forwards_private_tmpdir_without_extra_environment(tmp_path, local_venv):
    source_root = Path(__file__).resolve().parents[2]
    # Execute an unchanged copy of the real shell entrypoint in a disposable
    # layout. A developer's .venv takes precedence over HERMES_PYTHON; using
    # the real checkout here would accidentally launch the entire test suite.
    root = tmp_path / 'runner-root'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(source_root / 'scripts/run_tests.sh', root / 'scripts/run_tests.sh')
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
    if local_venv:
        bin_dir = root / '.venv/bin'
        bin_dir.mkdir(parents=True)
        (bin_dir / 'activate').write_text('')
        (bin_dir / 'python').symlink_to(interpreter)
    result = subprocess.run(['bash', str(root/'scripts/run_tests.sh')],
        env={'PATH':os.environ['PATH'], 'HOME':str(home), 'HERMES_PYTHON':str(interpreter),
             'TMPDIR':str(scratch), 'TEST_NOT_ALLOWED':'must-not-forward'},
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {'tmpdir':str(scratch), 'extra':None}

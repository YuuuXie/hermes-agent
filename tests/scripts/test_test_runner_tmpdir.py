"""Scratch placement through the canonical shell and its real pytest worker."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.run_tests_parallel import _runner_scratch_root


@pytest.mark.platforms("posix")
def test_canonical_runner_keeps_workers_inside_the_selected_tmpdir(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    scratch = tmp_path / "private scratch"
    scratch.mkdir()
    probe = tmp_path / "test_scratch_probe.py"
    probe.write_text(
        textwrap.dedent("""\
            import os
            import tempfile
            from pathlib import Path

            def test_worker_scratch(tmp_path):
                selected = Path(__file__).with_name("private scratch")
                worker = Path(os.environ["TMPDIR"])
                assert selected in worker.parents
                assert worker == Path(os.environ["PYTEST_DEBUG_TEMPROOT"])
                assert worker in tmp_path.parents
                assert Path(tempfile.gettempdir()) == worker
                assert "TEST_NOT_ALLOWED" not in os.environ
                (worker / "probe-artifact").touch()
            """),
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), str(probe), "-j", "1", "--file-retries=0", "-q"],
        cwd=root,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "HERMES_HOME": str(home / "hermes"),
            "HERMES_PYTHON": sys.executable,
            "TMPDIR": str(scratch),
            "TEST_NOT_ALLOWED": "must-not-forward",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout
    assert not list(scratch.rglob("probe-artifact"))


@pytest.mark.platforms("posix")
@pytest.mark.skipif(not Path("/var/tmp").is_dir(), reason="no FHS scratch directory")
@pytest.mark.parametrize("hermes_exported", [False, True])
def test_scratch_defaults_to_disk_without_a_caller_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hermes_exported: bool,
) -> None:
    monkeypatch.delenv("TMPDIR", raising=False)
    if hermes_exported:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("HERMES_SCRATCH_DIR", str(tmp_path))
    monkeypatch.setattr(os, "makedirs", lambda path, **kwargs: None)

    assert Path(_runner_scratch_root()).parent == Path("/var/tmp")

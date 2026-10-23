"""Tests for blocked-command recovery guidance (parser-limit + backgrounding)."""


from tools.approval import _hardline_block_result
from tools.approval_detection import _PARSER_LIMIT_DESCRIPTION
from tools.terminal_tool import _foreground_background_guidance
from tools import approval_floors


class TestParserLimitRecovery:
    def test_parser_limit_block_saves_payload_and_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        cmd = "python3 -c '" + "x = 1; " * 900 + "'"
        r = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, cmd)
        assert r["approved"] is False
        assert "RECOVERY" in r["message"]
        assert "blocked-scripts" in r["message"]
        import re as _re
        m = _re.search(r"saved to (\S+\.sh)", r["message"])
        assert m, r["message"]
        from pathlib import Path
        saved = Path(m.group(1))
        assert saved.exists()
        body = saved.read_text()
        assert cmd in body
        assert body.startswith("#!/usr/bin/env bash")
        assert f"bash {saved}" in r["message"]

    def test_save_failure_falls_back_to_manual_recipe(self, monkeypatch):
        monkeypatch.setattr(approval_floors, "_save_blocked_payload", lambda c: None)
        r = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, "python3 -c 'x'")
        assert "write_file" in r["message"]
        assert "bash /path/script.sh" in r["message"]



    def test_real_hardline_blocks_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = _hardline_block_result("recursive delete of root filesystem", "rm -rf --no-preserve-root /")
        assert "RECOVERY" not in r["message"]
        assert "unconditional blocklist" in r["message"]
        # And nothing was saved for a genuine hardline block.
        assert not (tmp_path / ".hermes" / "cache" / "blocked-scripts").exists()

    def test_old_saved_payloads_cleaned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        import os
        d = tmp_path / ".hermes" / "cache" / "blocked-scripts"
        d.mkdir(parents=True)
        stale = d / "blocked-1-dead.sh"
        stale.write_text("old")
        os.utime(stale, (1, 1))
        _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, "python3 -c 'y'")
        assert not stale.exists()


class TestBackgroundGuidanceRecipes:
    def test_ampersand_block_names_exact_call_shape(self):
        msg = _foreground_background_guidance("python3 server.py &")
        assert msg is not None
        assert "WITHOUT the '&'" in msg
        assert "background=true" in msg

    def test_nohup_block_names_exact_call_shape(self):
        msg = _foreground_background_guidance("nohup ./worker.sh > /dev/null 2>&1")
        assert msg is not None
        assert "WITHOUT the wrapper" in msg
        assert "notify_on_complete=true" in msg

    def test_plain_command_unaffected(self):
        assert _foreground_background_guidance("echo hello") is None

    def test_quoted_ampersand_not_flagged(self):
        assert _foreground_background_guidance('git commit -m "a & b"') is None

    def test_package_operations_name_binaries_without_launching_them(self):
        for command in (
            "npm install nodemon", "npm add nodemon --save-dev",
            "pnpm add vite", "yarn add gunicorn", "bun install uvicorn",
            "pip install uvicorn", "pip3 uninstall gunicorn",
            "python3 -m pip download uvicorn", "python -m pip wheel gunicorn",
            "pip hash uvicorn", "pip check uvicorn", "apt-get install gunicorn",
            "conda install uvicorn", "brew install nodemon", "apk add uvicorn",
        ):
            assert _foreground_background_guidance(command) is None, command

    def test_package_operation_does_not_hide_later_service(self):
        for command in (
            "npm install nodemon && npm start",
            "pip install uvicorn; uvicorn app:app",
            "pip check uvicorn || gunicorn app:app",
            "npm add nodemon | nodemon app.js",
            "pip wheel gunicorn\npython3 -m http.server",
        ):
            message = _foreground_background_guidance(command)
            assert message is not None and "long-lived server" in message, command

    def test_non_package_service_with_install_words_still_warns(self):
        message = _foreground_background_guidance("uvicorn app:app --note pip install")
        assert message is not None and "long-lived server" in message

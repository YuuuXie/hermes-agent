"""Online SQLite backups retain one generation and never publish missing DBs."""
import sqlite3
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import backup
from hermes_cli import backup_sqlite


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sample (value TEXT)")
    (home / "config.yaml").write_text("model: {}")
    return home


def test_missing_database_snapshot_preserves_existing_archive(tmp_path):
    home = _home(tmp_path)
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"previous archive")
    with patch.object(backup, "_safe_copy_db", return_value=False):
        with pytest.raises(SystemExit) as raised:
            backup._run_backup_locked(SimpleNamespace(output=str(archive), keep=0), home)
        assert raised.value.code != 0
        assert backup._write_full_zip_backup_locked(archive, home) is None
    assert archive.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".*.partial"))


def test_database_zip_failure_preserves_existing_archive(tmp_path):
    home = _home(tmp_path)
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"previous archive")
    real_write = zipfile.ZipFile.write
    reached = []

    def fail_db_write(zf, filename, arcname=None, *args, **kwargs):
        if arcname == "state.db":
            reached.append(True)
            raise OSError("staged DB write failed")
        return real_write(zf, filename, arcname, *args, **kwargs)

    with patch.object(zipfile.ZipFile, "write", fail_db_write):
        with pytest.raises(SystemExit) as raised:
            backup._run_backup_locked(SimpleNamespace(output=str(archive), keep=0), home)
        assert raised.value.code != 0
        assert backup._write_full_zip_backup_locked(archive, home) is None
    assert len(reached) == 2
    assert archive.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".*.partial"))


def test_read_pin_retries_rollback_exclusive_lock(tmp_path):
    src, dst = tmp_path / "source.db", tmp_path / "snapshot.db"
    connect = sqlite3.connect
    with connect(src) as db:
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('retained')")
    holder = connect(src, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    attempted = threading.Event()
    released = threading.Event()

    class ObservedSource(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    attempted.set()
                raise

    def open_db(database, *args, **kwargs):
        if kwargs.get("uri"):
            kwargs["factory"] = ObservedSource
        return connect(database, *args, **kwargs)

    def release_on_attempt():
        if attempted.wait(3):
            holder.rollback()
            released.set()

    releaser = threading.Thread(target=release_on_attempt)
    releaser.start()
    try:
        with patch.object(backup_sqlite.sqlite3, "connect", side_effect=open_db):
            assert backup_sqlite._safe_copy_db(src, dst, timeout_seconds=2)
    finally:
        attempted.set()
        releaser.join(timeout=3)
        holder.close()
    assert not releaser.is_alive() and released.is_set()
    with connect(dst) as snapshot:
        assert snapshot.execute("SELECT value FROM sample").fetchone() == ("retained",)


def test_wal_snapshot_keeps_initial_generation_during_writes(tmp_path):
    src, dst = tmp_path / "source.db", tmp_path / "snapshot.db"
    connect = sqlite3.connect
    writer = connect(src)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, payload BLOB)")
        writer.executemany("INSERT INTO records(payload) VALUES (?)", [(b"x" * 4096,)] * 1500)
        writer.commit()
        page_count = writer.execute("PRAGMA page_count").fetchone()[0]
        calls = []

        class ActiveSource(sqlite3.Connection):
            def backup(self, destination, *, pages=-1, progress=None, name="main", sleep=0.25):
                def interleave(status, remaining, total):
                    calls.append((status, remaining, total))
                    if remaining and len(calls) <= 20:
                        writer.execute("UPDATE records SET payload=? WHERE id=1", (str(len(calls)).encode(),))
                        writer.commit()
                    if progress:
                        progress(status, remaining, total)
                return super().backup(destination, pages=pages, progress=interleave, name=name, sleep=sleep)

        def open_db(database, *args, **kwargs):
            if kwargs.get("uri"):
                kwargs["factory"] = ActiveSource
            return connect(database, *args, **kwargs)

        with patch.object(backup_sqlite.sqlite3, "connect", side_effect=open_db):
            assert backup_sqlite._safe_copy_db(src, dst)
    finally:
        writer.close()
    with connect(dst) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert snapshot.execute("SELECT payload FROM records WHERE id=1").fetchone() == (b"x" * 4096,)
    assert len(calls) <= (page_count + 255) // 256

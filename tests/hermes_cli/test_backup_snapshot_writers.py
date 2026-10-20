"""Real SQLite WAL regression: ongoing writes must not restart snapshots."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import hermes_cli.backup as backup


class SnapshotTests(unittest.TestCase):
    def test_full_backup_refuses_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)/'home'
            home.mkdir()
            with sqlite3.connect(home/'state.db') as db:
                db.execute('CREATE TABLE sample (value TEXT)')
            archive = Path(tmp)/'archive.zip'
            archive.write_bytes(b'previous-verified-artifact')
            with patch.object(backup, '_safe_copy_db', return_value=False):
                with self.assertRaises(SystemExit) as raised:
                    backup._run_backup_locked(SimpleNamespace(output=str(archive),keep=0), home)
            self.assertNotEqual(raised.exception.code, 0)
            self.assertEqual(archive.read_bytes(), b'previous-verified-artifact')

    def test_db_zip_write_error_aborts_full_and_automatic_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            home.mkdir()
            with sqlite3.connect(home / 'state.db') as db:
                db.execute('CREATE TABLE sample (value TEXT)')
            (home / 'config.yaml').write_text('model: {}')
            archive = Path(tmp) / 'archive.zip'
            old_bytes = b'previous-verified-artifact'
            real_write = zipfile.ZipFile.write
            reached_db_write = []

            def fail_db_write(zf, filename, arcname=None, *args, **kwargs):
                if arcname == 'state.db':
                    reached_db_write.append(True)
                    raise OSError('staged db zip read failed')
                return real_write(zf, filename, arcname, *args, **kwargs)

            with patch.object(zipfile.ZipFile, 'write', fail_db_write):
                archive.write_bytes(old_bytes)
                with self.assertRaises(SystemExit) as raised:
                    backup._run_backup_locked(SimpleNamespace(output=str(archive), keep=0), home)
                self.assertNotEqual(raised.exception.code, 0)
                self.assertEqual(archive.read_bytes(), old_bytes)
                self.assertEqual(backup._write_full_zip_backup_locked(archive, home), None)
                self.assertEqual(archive.read_bytes(), old_bytes)
            self.assertEqual(len(reached_db_write), 2)
            self.assertEqual(list(Path(tmp).glob('.*.partial')), [])

    def test_rollback_journal_read_pin_waits_for_released_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / 'source.db', Path(tmp) / 'snapshot.db'
            connect = sqlite3.connect
            with connect(src) as db:
                db.execute('CREATE TABLE sample (value TEXT)')
                db.execute('INSERT INTO sample VALUES (?)', ('retained',))
            holder = connect(src, check_same_thread=False)
            holder.execute('BEGIN EXCLUSIVE')
            attempted = threading.Event()
            released = threading.Event()

            def release_on_attempt():
                if attempted.wait(3):
                    holder.rollback()
                    released.set()

            class ObservedSource(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    try:
                        return super().execute(sql, *args, **kwargs)
                    except sqlite3.OperationalError as exc:
                        if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            attempted.set()
                        raise

            def open_db(database, *args, **kwargs):
                if kwargs.get('uri'):
                    kwargs['factory'] = ObservedSource
                return connect(database, *args, **kwargs)

            releaser = threading.Thread(target=release_on_attempt)
            releaser.start()
            try:
                with patch.object(backup.sqlite3, 'connect', side_effect=open_db):
                    self.assertTrue(backup._safe_copy_db(src, dst, timeout_seconds=2.0))
            finally:
                attempted.set()
                releaser.join(timeout=3)
                holder.close()
            self.assertFalse(releaser.is_alive())
            self.assertTrue(released.is_set())
            with connect(dst) as snapshot:
                self.assertEqual(snapshot.execute('SELECT value FROM sample').fetchone()[0], 'retained')

    def test_snapshot_is_pinned_under_continuous_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp)/'source.db', Path(tmp)/'snapshot.db'
            connect = sqlite3.connect
            writer = connect(src)
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, payload BLOB)')
            writer.executemany('INSERT INTO records(payload) VALUES (?)', [(b'x'*4096,)]*1500)
            writer.commit()
            page_count = writer.execute('PRAGMA page_count').fetchone()[0]
            calls = []

            class ActiveSource(sqlite3.Connection):
                def backup(self, destination, *, pages=-1, progress=None, name='main', sleep=0.25):
                    def interleave(status, remaining, total):
                        calls.append((status,remaining,total))
                        if remaining and len(calls) <= 20:
                            writer.execute('UPDATE records SET payload=? WHERE id=1', (str(len(calls)).encode(),))
                            writer.commit()
                        if progress:
                            progress(status,remaining,total)
                    return super().backup(destination,pages=pages,progress=interleave,name=name,sleep=sleep)

            def open_db(database, *args, **kwargs):
                if kwargs.get('uri'):
                    kwargs['factory'] = ActiveSource
                return connect(database,*args,**kwargs)

            with patch.object(backup.sqlite3,'connect',side_effect=open_db):
                self.assertTrue(backup._safe_copy_db(src,dst))
            writer.close()
            with connect(dst) as snapshot:
                self.assertEqual(snapshot.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(snapshot.execute('SELECT count(*) FROM records').fetchone()[0], 1500)
                # An actual read snapshot must contain the initial value, never a later generation.
                self.assertEqual(snapshot.execute('SELECT payload FROM records WHERE id=1').fetchone()[0], b'x'*4096)
            self.assertLessEqual(len(calls), (page_count+255)//256)

if __name__ == '__main__':
    unittest.main()

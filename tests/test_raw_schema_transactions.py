"""Raw state schema creation is one durable publication, not per-table commits."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_state_schema as schema


def test_raw_schema_creation_is_not_partially_visible(tmp_path):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path)
    observer = sqlite3.connect(path)
    partial = []

    def inspect_ddl(sql):
        if sql.lstrip().upper().startswith('CREATE '):
            tables = observer.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                partial.append(tables)

    conn.set_trace_callback(inspect_ddl)
    try:
        schema.reconcile_state_schema(conn)
        assert not partial, 'A sibling observed a partially initialized canonical schema'
        assert observer.execute("SELECT name FROM sqlite_master WHERE name='async_delegations'").fetchone()
        assert not conn.in_transaction
    finally:
        conn.close()
        observer.close()


def test_raw_schema_ddl_error_rolls_back_creation(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path)
    monkeypatch.setattr(schema, 'SCHEMA_SQL', schema.SCHEMA_SQL + '\nINVALID INJECTED SQL;')
    try:
        with pytest.raises(sqlite3.OperationalError):
            schema.reconcile_state_schema(conn)
        assert not conn.in_transaction
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
def test_current_schema_open_does_not_require_writer_lock(tmp_path, mode):
    path = tmp_path / 'state.db'
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=' + mode)
    schema.reconcile_state_schema(writer)
    writer.execute('BEGIN IMMEDIATE')
    reader = sqlite3.connect(path, timeout=0.1)
    try:
        schema.reconcile_state_schema(reader)
        assert not reader.in_transaction
        assert reader.execute('PRAGMA journal_mode').fetchone()[0] == mode.lower()
    finally:
        reader.close()
        writer.rollback()
        writer.close()


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
def test_concurrent_cold_schema_openers_converge(tmp_path, mode):
    path = tmp_path / 'state.db'
    seed = sqlite3.connect(path)
    try:
        seed.execute('PRAGMA journal_mode=' + mode)
    finally:
        seed.close()
    start = threading.Barrier(4)

    def initialize():
        conn = sqlite3.connect(path, timeout=5)
        try:
            start.wait(timeout=5)
            schema.reconcile_state_schema(conn)
            assert not conn.in_transaction
            return conn.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: initialize(), range(4)))
    assert results[0]
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
def test_stale_schema_waits_for_transient_writer(tmp_path, mode):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path, timeout=2)
    conn.execute('PRAGMA journal_mode=' + mode)
    schema.reconcile_state_schema(conn)
    conn.execute('DROP TABLE async_delegations')
    conn.commit()
    locked, release = threading.Event(), threading.Event()

    def hold_writer():
        other = sqlite3.connect(path)
        try:
            other.execute('BEGIN IMMEDIATE')
            locked.set()
            assert release.wait(5)
        finally:
            other.rollback()
            other.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_writer)
        timer = threading.Timer(0.2, release.set)
        try:
            assert locked.wait(5)
            timer.start()
            schema.reconcile_state_schema(conn)
            assert release.is_set(), 'Schema publication cannot pass the active writer'
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='async_delegations'").fetchone()
            assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 2000
            assert not conn.in_transaction
        finally:
            release.set()
            timer.cancel()
            timer.join()
            holder.result(timeout=5)
            conn.close()


def test_stale_schema_exhausted_busy_wait_preserves_connection_settings(tmp_path):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path, timeout=0.03)
    schema.reconcile_state_schema(conn)
    conn.execute('DROP TABLE async_delegations')
    conn.commit()
    writer = sqlite3.connect(path)
    writer.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            schema.reconcile_state_schema(conn)
        assert not conn.in_transaction
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='async_delegations'").fetchone()
    finally:
        writer.rollback()
        writer.close()
        conn.close()

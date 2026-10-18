"""SQL dialect tests always run; use --postgres-test-url for live protocol tests.

CI/local URL must point to a disposable database: its bfixf schema is emptied.
Never point this test option at the production Supabase project.
"""
import json
import os
from pathlib import Path
import runpy
import sqlite3

import pytest
from app import config as cfg, database_config
from app.postgres import schema_metadata, translate, ddl
from app.storage import Database, SCHEMA, db

LOCAL_URI = 'postgresql://postgres:postgres@127.0.0.1:55432/postgres?sslmode=disable'


@pytest.fixture
def metadata():
    return schema_metadata(SCHEMA)


def test_schema_uses_64bit_ids_and_native_sequences():
    result = ddl('id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ts REAL')
    assert result == 'id BIGSERIAL PRIMARY KEY, chat_id BIGINT, ts DOUBLE PRECISION'


def test_placeholder_and_like_literals(metadata):
    sql, auto = translate("SELECT * FROM files WHERE name LIKE '/admin%' AND name != '?' AND id=?", metadata)
    assert "LIKE '/admin%%'" in sql and "!= '?'" in sql and 'id=%s' in sql
    assert not auto


def test_nested_upsert_expressions_are_qualified(metadata):
    sql, _ = translate("INSERT INTO chat_peers(peer_id,title,access_hash) VALUES(?,?,?) ON CONFLICT(peer_id) DO UPDATE SET title=COALESCE(NULLIF(excluded.title,''), title),access_hash=COALESCE(excluded.access_hash,access_hash)", metadata)
    assert 'title=COALESCE' in sql and ',access_hash=COALESCE' in sql
    assert 'chat_peers.title)' in sql and 'chat_peers.access_hash)' in sql
    assert ',chat_peers.access_hash=' not in sql


def test_replace_supports_unique_token_and_composite_keys(metadata):
    sql, auto = translate('INSERT OR REPLACE INTO campaign_links(campaign_id,token) VALUES(?,?)', metadata)
    assert 'ON CONFLICT (token)' in sql and 'RETURNING id' in sql and auto
    sql, _ = translate('INSERT OR REPLACE INTO campaign_media(campaign_id,idx,name) VALUES(?,?,?)', metadata)
    assert 'ON CONFLICT (campaign_id,idx)' in sql


def test_dates_and_binary_max(metadata):
    sql, _ = translate("SELECT date(ts,'unixepoch','+6 hours') FROM events", metadata)
    assert "AT TIME ZONE 'UTC' + interval '6 hours'" in sql
    sql, _ = translate('UPDATE video_copy_items SET attempts=MAX(0,attempts-1)', metadata)
    assert 'GREATEST(0,attempts-1)' in sql


def test_private_configuration_requires_no_environment(tmp_path, monkeypatch):
    package = tmp_path / 'app'
    package.mkdir()
    target = package / 'config.py'
    target.write_bytes(Path('app/config.py').read_bytes())
    # Test the file path branch without relying on a platform-specific mount.
    target.write_text(target.read_text().replace("Path('/var/data').is_dir()", 'False'))
    (tmp_path / 'database.private.json').write_text(json.dumps({'database_url': LOCAL_URI, 'sslmode': 'prefer'}))
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('PG_SSLMODE', raising=False)
    result = runpy.run_path(str(target))
    assert result['DB_FILE'] == LOCAL_URI
    assert result['PG_SSLMODE'] == 'prefer'


def test_private_file_is_restricted_and_validated(tmp_path):
    target = tmp_path / 'database.private.json'
    config = {'database_url': 'postgresql://user:private@db.example/postgres', 'sslmode': 'prefer'}
    database_config.save(config, target)
    assert database_config.parse(target.read_bytes()) == config
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        database_config.parse({'database_url': 'sqlite:///wrong', 'sslmode': 'prefer'})
    with pytest.raises(ValueError):
        database_config.parse(b'x' * 16385)


@pytest.fixture
def pg(request, monkeypatch):
    uri = request.config.getoption('--postgres-test-url')
    if not uri:
        pytest.skip('Pass --postgres-test-url for disposable PostgreSQL integration tests')
    database = Database(uri)
    database.conn.raw.execute('TRUNCATE '+','.join(database.conn.metadata)+' RESTART IDENTITY')
    old = db._impl
    db._impl = database
    try:
        yield database
    finally:
        db._impl = old
        database.conn.close()


def test_pg_bigint_upsert_analytics_and_json(pg):
    uid = 1234567890123
    pg.touch_user(uid, 'বাংলা', 'visitor')
    pg.set_chat_peer(-1001234567890, 7123456789012345678, 'Original')
    pg.set_chat_peer(-1001234567890, None, 'Updated')
    assert pg.chat_peer(-1001234567890)['access_hash'] == 7123456789012345678
    store = pg.create_store(uid, 'Videos')
    fid = pg.add_file(store['id'], 'Video', 'Video', -1001234567890, 1)
    pg.log_event('deliver', uid, store['id'], fid)
    assert pg.user_analytics(uid)['videos'][0]['id'] == fid
    assert pg.store_analytics(store['id'])['watchers'][0]['user_id'] == uid
    assert pg.file_watchers(fid)[0]['user_id'] == uid
    assert pg.events_by_day()
    json.dumps(pg.stats())  # no Decimal serialization failures
    assert 'postgres:postgres@' not in pg.display_path


def test_pg_snapshot_restore_preserves_ids_settings_hashes_and_sequences(pg, tmp_path, monkeypatch):
    from app.services import portable
    monkeypatch.setattr(cfg, 'BACKUP_DIR', str(tmp_path))
    pg.touch_user(77, 'Old user', 'old')
    pg._run('UPDATE users SET access_hash=? WHERE user_id=?', (7123456789012345678, 77))
    store = pg.create_store(77, 'Original')
    fid = pg.add_file(store['id'], 'Original video', 'Video', -100123, 99)
    pg.set_meta('cred:BOT_TOKEN', 'private-test-token')
    pg.set_meta('cred:API_ID', '123')
    pg.set_meta('cred:API_HASH', 'private-test-hash')
    backup = portable.export_bytes()
    pg.touch_user(88, 'Temporary', 'gone')
    result = portable.restore(backup)
    assert result['ok'] and Path(result['safety_copy']).exists()
    assert pg.user(88) is None and pg.user(77)['access_hash'] == 7123456789012345678
    assert pg.file(fid)['msg_id'] == 99 and pg.get_meta('cred:BOT_TOKEN') == 'private-test-token'
    assert pg.add_file(store['id'], 'Next video', 'Video', -100123, 100) > fid


def test_pg_failed_import_rolls_back_data(pg):
    pg.touch_user(77, 'Must survive', 'safe')
    source = Database(':memory:')
    try:
        source.touch_user(1, 'First', 'first')
        # Valid SQLite, but invalid PostgreSQL BIGINT data: fail after truncation.
        source._run('UPDATE users SET access_hash=? WHERE user_id=1', ('not-an-integer',))
        with pytest.raises(Exception):
            pg.conn.import_sqlite(source.conn.serialize(), replace=True)
        assert pg.user(77)['name'] == 'Must survive'
        assert pg.user(1) is None
    finally:
        source.conn.close()


def test_pg_import_refuses_populated_destination(pg):
    pg.touch_user(77, 'Must survive', 'safe')
    source = Database(':memory:')
    try:
        with pytest.raises(ValueError, match='already contains'):
            pg.conn.import_sqlite(source.conn.serialize())
        assert pg.user(77)
    finally:
        source.conn.close()


def test_pg_bootstrap_preserves_local_sqlite_and_does_not_repeat(pg, tmp_path, monkeypatch):
    from app.storage import _LazyDB
    source_path = tmp_path / 'old.sqlite3'
    source = Database(str(source_path))
    source.touch_user(12, 'Original install', 'old')
    source.conn.close()
    monkeypatch.setattr(cfg, 'SQLITE_BOOTSTRAP_FILE', str(source_path))
    monkeypatch.setattr(cfg, 'DB_FILE', pg.path)
    lazy = _LazyDB()
    migrated = lazy.bind(pg.path)
    try:
        assert migrated.user(12)['name'] == 'Original install'
        assert source_path.is_file()
        migrated.touch_user(13, 'New PG user', 'new')
        second = lazy.bind(pg.path)
        try:
            assert second.user(13) and second.user(12)
        finally:
            second.conn.close()
    finally:
        migrated.conn.close()


def test_postgres_startup_outage_uses_only_a_recovery_database(tmp_path, monkeypatch):
    import run
    monkeypatch.setattr(cfg, 'DB_FILE', 'postgresql://test:private@unreachable.example/postgres')
    monkeypatch.setattr(cfg, 'DATABASE_UNAVAILABLE', False)
    original = db.bind
    old = db._impl
    def bind(path):
        if path.startswith('postgresql:'):
            raise ConnectionError('offline')
        return original(path)
    monkeypatch.setattr(db, 'bind', bind)
    try:
        assert run.initialize_database() is False
        assert cfg.DATABASE_UNAVAILABLE is True
        assert db.path == ':memory:'
        assert not (tmp_path / 'bot_data.sqlite3').exists()
    finally:
        db.conn.close()
        db._impl = old


def test_connection_package_can_be_uploaded_without_unpacking():
    import io, zipfile
    buffer = io.BytesIO()
    config = {'database_url': 'postgresql://test:private@db.example/postgres', 'sslmode': 'prefer'}
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('database.private.json', json.dumps(config))
    assert database_config.parse(buffer.getvalue()) == config
    with pytest.raises(ValueError):
        database_config.parse(b'PK this is not a zip')

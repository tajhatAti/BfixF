"""SQLite-only startup must not activate a stale remote connection setting."""
import os
from pathlib import Path
import subprocess
import sys


def test_local_database_setting_wins_over_obsolete_remote_environment(tmp_path):
    target = tmp_path / 'persistent.sqlite3'
    env = dict(os.environ, DB_FILE=str(target), DATABASE_URL='obsolete://ignored')
    result = subprocess.run(
        [sys.executable, '-c', 'from app import config; print(config.DB_FILE)'],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == str(target)


def test_network_url_is_never_used_as_sqlite_filename():
    env = dict(os.environ, DB_FILE='obsolete://not-a-file')
    result = subprocess.run(
        [sys.executable, '-c', 'from app import config; print(config.DB_FILE)'],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=True)
    assert result.stdout.strip().endswith('bot_data.sqlite3')
    assert '://' not in result.stdout

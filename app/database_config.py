"""Private PostgreSQL setup: JSON file or website upload, no environment required."""
import json
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit


def parse(value):
    if isinstance(value, bytes):
        if len(value) > 16384:
            raise ValueError('Configuration file is too large')
        if value.startswith(b'PK'):
            import io
            import zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(value)) as archive:
                    entry = archive.getinfo('database.private.json')
                    if entry.file_size > 16384:
                        raise ValueError('Configuration file is too large')
                    value = archive.read(entry)
            except (zipfile.BadZipFile, KeyError, OSError, RuntimeError) as exc:
                raise ValueError('Invalid private connection package') from None
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('A JSON configuration object is required')
    uri = str(value.get('database_url') or '').strip()
    parts = urlsplit(uri)
    if parts.scheme not in ('postgresql', 'postgres') or not parts.hostname or not parts.username:
        raise ValueError('A valid PostgreSQL connection URL is required')
    if parts.port and not 0 < parts.port < 65536:
        raise ValueError('Invalid database port')
    mode = str(value.get('sslmode') or 'require')
    if mode not in ('require', 'prefer', 'verify-ca', 'verify-full'):
        raise ValueError('Unsupported TLS mode')
    return {'database_url': uri, 'sslmode': mode}


def save(config, path):
    config = parse(config)
    path = Path(path)
    temporary = path.with_suffix('.incoming')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def probe(config):
    """Authenticate only: never touch the provider's existing tables."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    sslmode = conninfo_to_dict(config['database_url']).get('sslmode', config['sslmode'])
    with psycopg.connect(config['database_url'], sslmode=sslmode, connect_timeout=15,
                         prepare_threshold=None) as conn:
        conn.execute('SELECT 1').fetchone()


_switch_lock = None

def switch_lock():
    global _switch_lock
    if _switch_lock is None:
        _switch_lock = asyncio.Lock()
    return _switch_lock

"""Visible build identity, including ZIP deployments without .git."""
import hashlib
from functools import lru_cache
from pathlib import Path

BUILD = 'desi-2026.10.18-supabase'


@lru_cache(maxsize=1)
def identity():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return f'{BUILD} / {digest.hexdigest()[:10]}'

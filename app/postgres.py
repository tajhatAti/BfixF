"""PostgreSQL connection adapter for the application's existing storage queries.

Keeps IDs as BIGINT, parameter binding (never interpolation), atomic imports and
SQLite-compatible rows/backups. Uses a dedicated schema, not Supabase public tables.
"""
from contextlib import contextmanager
import re
import sqlite3
import threading

SCHEMA_NAME = 'bfixf'


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, rows=(), rowcount=0, lastrowid=None):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.offset = 0
    def fetchone(self):
        if self.offset >= len(self.rows):
            return None
        item = self.rows[self.offset]
        self.offset += 1
        return item
    def fetchall(self):
        items = self.rows[self.offset:]
        self.offset = len(self.rows)
        return items
    def __iter__(self):
        return iter(self.fetchall())


def sqlite_template(schema):
    from app.storage import Database
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    temporary = object.__new__(Database)
    temporary.conn = conn
    temporary._upgrade()
    return conn


def schema_metadata(schema):
    from contextlib import closing
    with closing(sqlite_template(schema)) as conn:
        result = {}
        for (table, ddl) in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
            result[table] = {'columns': [c[1] for c in columns],
                             'pk': [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]],
                             'unique': [[c[2] for c in conn.execute(f'PRAGMA index_info("{idx[1]}")')]
                                        for idx in conn.execute(f'PRAGMA index_list("{table}")') if idx[2]],
                             'auto': 'AUTOINCREMENT' in ddl.upper()}
        return result


def ddl(sql):
    sql = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY', sql, flags=re.I)
    sql = re.sub(r'\bINTEGER\b', 'BIGINT', sql, flags=re.I)
    return re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql, flags=re.I)


def split_assignments(text):
    parts, start, depth, quoted = [], 0, 0, False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "'":
            if quoted and i+1 < len(text) and text[i+1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            depth += (char == '(') - (char == ')')
            if char == ',' and depth == 0:
                parts.append(text[start:i]); start = i+1
        i += 1
    parts.append(text[start:])
    return parts


def translate(sql, metadata):
    """Translate only the SQLite dialect actually used by storage.py."""
    sql = sql.strip().rstrip(';')
    insert = re.match(r'INSERT\s+(?:OR\s+(IGNORE|REPLACE)\s+)?INTO\s+(\w+)\s*\(([^)]+)\)', sql, re.I)
    table = insert.group(2) if insert else None
    if insert and insert.group(1):
        mode = insert.group(1).upper()
        sql = re.sub(r'INSERT\s+OR\s+(IGNORE|REPLACE)', 'INSERT', sql, count=1, flags=re.I)
        if mode == 'IGNORE':
            sql += ' ON CONFLICT DO NOTHING'
        else:
            columns = [c.strip() for c in insert.group(3).split(',')]
            constraints = [metadata[table]['pk']] + metadata[table]['unique']
            pk = next((key for key in constraints if key and set(key) <= set(columns)), None)
            if not pk:
                raise ValueError('REPLACE requires a primary or unique key')
            # SQLite REPLACE resets omitted columns to defaults. Match that rather
            # than silently retaining old values on an incomplete restore import.
            assignments = [f'{c}=excluded.{c}' if c in columns else f'{c}=DEFAULT'
                           for c in metadata[table]['columns'] if c not in pk]
            sql += f" ON CONFLICT ({','.join(pk)}) DO UPDATE SET " + ','.join(assignments)
    if insert and 'DO UPDATE SET' in sql.upper():
        before, after = re.split(r'\bDO UPDATE SET\b', sql, maxsplit=1, flags=re.I)
        # Qualify existing-row references in expressions (e.g. attempts+1).
        assignments = split_assignments(after)
        for i, assignment in enumerate(assignments):
            if '=' not in assignment:
                continue
            lhs, rhs = assignment.split('=', 1)
            for column in metadata.get(table, {}).get('columns', []):
                rhs = re.sub(rf'(?<![\w.]){column}\b', f'{table}.{column}', rhs)
            assignments[i] = lhs + '=' + rhs
        sql = before + 'DO UPDATE SET' + ','.join(assignments)
    sql = re.sub(r"date\((\w+),\s*'unixepoch'(?:,\s*'\+6 hours')?\)",
                 lambda m: "to_char(to_timestamp(" + m[1] + ") AT TIME ZONE 'UTC'" +
                 (" + interval '6 hours'" if '+6 hours' in m[0] else '') + ", 'YYYY-MM-DD')", sql, flags=re.I)
    sql = re.sub(r'\bMAX\(([^()]+),([^()]+)\)', r'GREATEST(\1,\2)', sql, flags=re.I)
    sql = re.sub(r'\bMIN\(([^()]+),([^()]+)\)', r'LEAST(\1,\2)', sql, flags=re.I)
    sql = re.sub(r'GROUP_CONCAT\((\w+)\)', r"string_agg(\1::text, ',')", sql, flags=re.I)
    sql = re.sub(r'\bHAVING c >', 'HAVING COUNT(*) >', sql)
    # Preserve literals, including ? and percent characters in LIKE patterns.
    tokens = re.split(r"('(?:''|[^'])*')", sql)
    sql = ''.join(part.replace('%', '%%').replace('?', '%s') if i % 2 == 0 else part.replace('%', '%%')
                  for i, part in enumerate(tokens))
    auto = bool(table and metadata.get(table, {}).get('auto'))
    if auto and 'RETURNING' not in sql.upper():
        sql += ' RETURNING id'
    return sql, auto


class Connection:
    def __init__(self, uri, schema, sslmode='require'):
        import psycopg
        self.metadata = schema_metadata(schema)
        self.schema = schema
        self.lock = threading.RLock()
        from psycopg.conninfo import conninfo_to_dict
        sslmode = conninfo_to_dict(uri).get('sslmode', sslmode)
        self.raw = psycopg.connect(uri, sslmode=sslmode, connect_timeout=15,
                                   autocommit=True, prepare_threshold=None,
                                   cursor_factory=psycopg.ClientCursor)
        self.raw.execute(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}')
        self.raw.execute(f'SET search_path TO {SCHEMA_NAME}')
        self._contexts = threading.local()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.raw.transaction():
                yield

    def __enter__(self):
        context = self.transaction()
        if not hasattr(self._contexts, 'stack'):
            self._contexts.stack = []
        self._contexts.stack.append(context)
        return context.__enter__()
    def __exit__(self, *args):
        return self._contexts.stack.pop().__exit__(*args)

    def execute(self, sql, params=()):
        with self.lock:
            if re.search(r"FROM sqlite_master WHERE type='table'", sql):
                return self.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=? AND table_type='BASE TABLE'", (SCHEMA_NAME,))
            pragma = re.fullmatch(r'PRAGMA table_info\((\w+)\)', sql.strip(), re.I)
            if pragma:
                table = pragma[1]
                cursor = self.raw.execute('SELECT column_name FROM information_schema.columns '
                                          'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
                                          (SCHEMA_NAME, table))
                return Cursor([Row(cid=i, name=r[0]) for i, r in enumerate(cursor.fetchall())])
            translated, auto = translate(ddl(sql) if sql.lstrip().upper().startswith(('CREATE ', 'ALTER ')) else sql, self.metadata)
            cursor = self.raw.execute(translated, tuple(params))
            rows = []
            if cursor.description:
                names = [c.name for c in cursor.description]
                from decimal import Decimal
                rows = [Row(zip(names, [(int(v) if v == v.to_integral_value() else float(v))
                                       if isinstance(v, Decimal) else v for v in row]))
                        for row in cursor.fetchall()]
            return Cursor(rows, cursor.rowcount, rows[0]['id'] if auto and rows else None)

    def executemany(self, sql, values):
        with self.transaction():
            count = 0
            for params in values:
                count += max(0, self.execute(sql, params).rowcount)
            return Cursor(rowcount=count)

    def executescript(self, schema):
        for statement in schema.split(';'):
            # Strip line comments before checking/translating DDL.
            statement = re.sub(r'--[^\n]*', '', statement).strip()
            if statement:
                self.execute(statement)
    def commit(self):
        pass  # autocommit outside explicit transaction() blocks
    def rollback(self):
        self.raw.rollback()
    def close(self):
        self.raw.close()

    def snapshot(self):
        """Consistent, portable SQLite bytes; no pg_dump binary required."""
        target = sqlite_template(self.schema)
        try:
            with self.transaction():
                self.raw.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                for table, meta in self.metadata.items():
                    columns = meta['columns']
                    # PostgreSQL physical column order may differ after upgrades.
                    rows = self.raw.execute(f'SELECT {",".join(columns)} FROM "{table}"').fetchall()
                    if rows:
                        target.executemany(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" for c in columns)})', rows)
            target.commit()
            return target.serialize()
        finally:
            target.close()

    def import_sqlite(self, raw, *, replace=False):
        """Atomic import preserving IDs, hashes, sessions, settings and counters.

        Bootstrap refuses a nonempty destination. Restore replaces only our schema.
        Unknown tables/columns fail validation BEFORE any destination is modified.
        """
        source = sqlite3.connect(':memory:')
        try:
            # sqlite backup() includes all committed WAL pages, but serialize()
            # may retain the WAL header. The in-memory import has no WAL file.
            if raw.startswith(b'SQLite format 3\x00'):
                raw = raw[:18] + b'\x01\x01' + raw[20:]
            source.deserialize(raw)
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Invalid SQLite backup')
            tables = [r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if not {'users', 'stores', 'files'} <= set(tables):
                raise ValueError('Not a BfixF backup')
            unknown = set(tables) - self.metadata.keys()
            if unknown:
                raise ValueError('Unknown backup tables: ' + ', '.join(sorted(unknown)))
            data = {}
            for table in tables:
                columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
                if set(columns) - set(self.metadata[table]['columns']):
                    raise ValueError('Unknown columns in backup table ' + table)
                data[table] = (columns, list(source.execute(f'SELECT * FROM "{table}"')))
            with self.transaction():
                self.raw.execute('SELECT pg_advisory_xact_lock(182861, 1)')
                names = ','.join(f'"{t}"' for t in self.metadata)
                self.raw.execute('LOCK TABLE ' + names + ' IN ACCESS EXCLUSIVE MODE')
                nonempty = any(self.raw.execute(f'SELECT EXISTS(SELECT 1 FROM "{t}")').fetchone()[0]
                               for t in self.metadata)
                if nonempty and not replace:
                    raise ValueError('PostgreSQL already contains data; use confirmed restore instead')
                if replace:
                    self.raw.execute('TRUNCATE ' + names + ' RESTART IDENTITY')
                counts = {}
                for table, (columns, rows) in data.items():
                    if rows:
                        # Bounded multi-row INSERT: efficient on a remote pooler,
                        # stays below PostgreSQL's bind-parameter limit, and does
                        # not depend on optional protocol pipeline support.
                        width = max(1, min(500, 60000 // len(columns)))
                        placeholder = '(' + ','.join('%s' for c in columns) + ')'
                        for offset in range(0, len(rows), width):
                            batch = rows[offset:offset + width]
                            sql = f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ' + ','.join(placeholder for row in batch)
                            self.raw.execute(sql, tuple(value for row in batch for value in row))
                    counts[table] = len(rows)
                for table, meta in self.metadata.items():
                    if meta['auto']:
                        next_id = self.raw.execute(f'SELECT COALESCE(MAX(id),0)+1 FROM "{table}"').fetchone()[0]
                        self.raw.execute(f'ALTER SEQUENCE {SCHEMA_NAME}.{table}_id_seq RESTART WITH {int(next_id)}')
                return counts
        finally:
            source.close()

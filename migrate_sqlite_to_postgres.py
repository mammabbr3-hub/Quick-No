#!/usr/bin/env python3
"""One-time migration helper: copy the existing Mobile Business Hub SQLite DB to PostgreSQL.

Usage:
  SQLITE_PATH=/app/data/mobile.db DATABASE_URL='postgresql://...' python migrate_sqlite_to_postgres.py

The script creates tables from the SQLite schema, then copies all rows. It does not
modify or delete the SQLite source. Run it once before switching production to PostgreSQL.
"""
import os, re, sqlite3, sys
try:
    import psycopg
    from psycopg import sql
except Exception:
    print('Install psycopg[binary] before running this migration.', file=sys.stderr)
    raise

src = os.environ.get('SQLITE_PATH', os.environ.get('mobile_DB_PATH', 'mobile.db'))
dsn = os.environ.get('DATABASE_URL', '').strip()
if not dsn:
    raise SystemExit('DATABASE_URL is required')

sconn = sqlite3.connect(src)
sconn.row_factory = sqlite3.Row
pconn = psycopg.connect(dsn)

try:
    tables = sconn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
    with pconn.cursor() as cur:
        for t in tables:
            ddl = t['sql'] or ''
            ddl = ddl.replace('AUTOINCREMENT', '')
            ddl = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\b', 'BIGSERIAL PRIMARY KEY', ddl, flags=re.I)
            ddl = ddl.replace('INSERT OR IGNORE', 'INSERT')
            cur.execute(ddl)
        pconn.commit()

        for t in tables:
            name = t['name']
            rows = sconn.execute(f'SELECT * FROM "{name.replace(chr(34), chr(34)*2)}"').fetchall()
            if not rows:
                continue
            cols = [d[0] for d in sconn.execute(f'SELECT * FROM "{name.replace(chr(34), chr(34)*2)}" LIMIT 0').description]
            q = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
                sql.Identifier(name),
                sql.SQL(',').join(sql.Identifier(c) for c in cols),
                sql.SQL(',').join(sql.Placeholder() for _ in cols),
            )
            cur.executemany(q, [tuple(r[c] for c in cols) for r in rows])
            print(f'{name}: {len(rows)} rows')

        # Reset BIGSERIAL sequences after copying explicit SQLite primary-key values.
        # Without this, the next PostgreSQL INSERT could reuse an already-copied ID.
        for t in tables:
            name = t['name']
            safe_name = name.replace(chr(34), chr(34)*2)
            pk_cols = [r['name'] for r in sconn.execute(f'PRAGMA table_info("{safe_name}")').fetchall()
                       if r['pk'] and str(r['type']).upper().startswith('INTEGER')]
            for col in pk_cols:
                try:
                    cur.execute("SELECT pg_get_serial_sequence(%s,%s)", (name, col))
                    seq_row = cur.fetchone(); seq = seq_row[0] if seq_row else None
                    if not seq:
                        continue
                    cur.execute(sql.SQL('SELECT MAX({}) FROM {}').format(sql.Identifier(col), sql.Identifier(name)))
                    max_id = cur.fetchone()[0]
                    if max_id is None:
                        cur.execute('SELECT setval(%s, 1, false)', (seq,))
                    else:
                        cur.execute('SELECT setval(%s, %s, true)', (seq, int(max_id)))
                except Exception as exc:
                    print(f'sequence {name}.{col}: skipped ({exc})')

        pconn.commit()

        # Recreate SQLite's named indexes after data copy. Skip SQLite's internal
        # autoindexes and indexes whose DDL is unavailable.
        indexes = sconn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        for idx in indexes:
            try:
                ddl = idx['sql']
                cur.execute(ddl)
            except Exception as exc:
                print(f'index {idx["name"]}: skipped ({exc})')
        pconn.commit()
finally:
    sconn.close(); pconn.close()

print('Migration complete. The SQLite source was not modified.')

#!/usr/bin/env python3
"""Verify an already published release before treating a retry as complete."""
import hashlib
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path

directory = Path(sys.argv[1])
commit = sys.argv[2]
manifest = json.loads((directory / 'catalog-manifest.json').read_text(encoding='utf-8'))
database = directory / 'winprovision-catalog.sqlite'
with database.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
if (manifest.get('schemaVersion') not in (1, 2) or manifest.get('sourceCommit') != commit
        or manifest.get('database') != database.name or manifest.get('sha256') != digest
        or manifest.get('sizeBytes') != database.stat().st_size):
    raise SystemExit('Manifesto da release divergente')
with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
    metadata = dict(conn.execute('SELECT key, value FROM metadata'))
    columns = {row[1] for row in conn.execute('PRAGMA table_info(apps)')}
    if (conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
            or metadata.get('sourceCommit') != commit or int(metadata['schemaVersion']) != manifest['schemaVersion']
            or conn.execute('SELECT count(*) FROM apps').fetchone()[0] != manifest['appCount']):
        raise SystemExit('Índice publicado divergente')
    if manifest['schemaVersion'] >= 2 and 'app_sha256' not in columns:
        raise SystemExit('Índice v2 sem apps.app_sha256')

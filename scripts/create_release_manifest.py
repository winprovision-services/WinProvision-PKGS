#!/usr/bin/env python3
"""Create checksummed release metadata for a verified catalog index."""
import hashlib
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1]).resolve()
expected_commit = sys.argv[2]
with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('SQLite inválido')
    metadata = dict(conn.execute('SELECT key, value FROM metadata'))
    count = conn.execute('SELECT count(*) FROM apps').fetchone()[0]
    if metadata.get('sourceCommit') != expected_commit or count != int(metadata['appCount']):
        raise SystemExit('Origem ou quantidade não corresponde ao índice')
with database.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
manifest = {'schemaVersion': int(metadata['schemaVersion']), 'sourceCommit': expected_commit, 'database': database.name,
            'sha256': digest, 'sizeBytes': database.stat().st_size,
            'appCount': count, 'versionCount': int(metadata['versionCount'])}
manifest_path = database.parent / 'catalog-manifest.json'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
with manifest_path.open('rb') as stream:
    manifest_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
(database.parent / 'SHA256SUMS').write_text(f'{digest}  {database.name}\n{manifest_digest}  {manifest_path.name}\n', encoding='ascii')

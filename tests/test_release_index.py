import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class ReleaseIndexTests(unittest.TestCase):
    def test_pinned_commit_search_icons_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Test'], check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'test@example.com'], check=True)
            scripts = root / 'scripts'
            scripts.mkdir()
            shutil.copy(SCRIPTS / 'build_release_index.py', scripts)
            app = root / 'c/Contoso/App/app.json'
            app.parent.mkdir(parents=True)
            doc = {'id': 'Contoso.App', 'source': 'winget', 'package': {'name': 'Café App', 'tags': ['editor']},
                   'latestVersion': '1', 'versions': [{'version': '1'}],
                   'media': {'icons': [{'path': 'media/icon.aaaaaaaa.png', 'sha256': 'a' * 64, 'mimeType': 'image/png'}]}}
            app.write_text(json.dumps(doc), encoding='utf-8')
            subprocess.run(['git', '-C', str(root), 'add', 'c'], check=True)
            subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'first'], check=True)
            commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            doc['package']['name'] = 'Changed'
            app.write_text(json.dumps(doc), encoding='utf-8')
            subprocess.run(['git', '-C', str(root), 'commit', '-qam', 'second'], check=True)
            db = root / 'release/winprovision-catalog.sqlite'
            env = dict(os.environ, CATALOG_REF=commit, INDEX_DB_PATH=str(db))
            subprocess.run([sys.executable, str(scripts / 'build_release_index.py')], env=env, check=True, capture_output=True)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('select name, icon_sha256 from apps').fetchone(), ('Café App', 'a' * 64))
                self.assertEqual(conn.execute("select id from app_search where app_search match 'cafe'").fetchone()[0], 'Contoso.App')
                self.assertEqual(dict(conn.execute('select * from metadata'))['sourceCommit'], commit)
            subprocess.run([sys.executable, str(SCRIPTS / 'create_release_manifest.py'), str(db), commit], check=True)
            subprocess.run([sys.executable, str(SCRIPTS / 'verify_release_manifest.py'), str(db.parent), commit], check=True)
            manifest = db.parent / 'catalog-manifest.json'
            metadata = json.loads(manifest.read_text())
            metadata['sha256'] = '0' * 64
            manifest.write_text(json.dumps(metadata))
            result = subprocess.run([sys.executable, str(SCRIPTS / 'verify_release_manifest.py'), str(db.parent), commit], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

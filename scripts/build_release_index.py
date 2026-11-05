#!/usr/bin/env python3
"""Build a compact searchable SQLite index from a WinProvision catalog Git ref."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REF = os.environ.get("CATALOG_REF", "manifests")
OUTPUT = Path(os.environ.get("INDEX_DB_PATH", str(ROOT / "index.db"))).resolve()
BATCH_SIZE = 1000


class BuildError(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", "-C", str(ROOT), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise BuildError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def tree_entries(commit: str) -> Iterator[tuple[str, str]]:
    raw = git("ls-tree", "-r", "-z", "--full-tree", commit)
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, path_bytes = entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        path = path_bytes.decode("utf-8", "strict")
        if (object_type == "blob" and mode in {"100644", "100755"}
                and path.endswith("/app.json") and "/versions/" not in path):
            yield object_id, path


def blob_stream(entries: list[tuple[str, str]]) -> Iterator[tuple[str, bytes]]:
    process = subprocess.Popen(["git", "-C", str(ROOT), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None
    try:
        for object_id, path in entries:
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise BuildError(f"Objeto Git inválido para {path}")
            size = int(header[2])
            content = process.stdout.read(size)
            if len(content) != size or process.stdout.read(1) != b"\n":
                raise BuildError(f"Blob incompleto: {path}")
            yield path, content
    finally:
        process.stdin.close()
        process.wait()
        if process.returncode:
            error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise BuildError(f"git cat-file falhou: {error.strip()}")
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE apps (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT,
    publisher TEXT,
    description TEXT,
    long_description TEXT,
    homepage TEXT,
    package_url TEXT,
    moniker TEXT,
    locale TEXT,
    license TEXT,
    latest_version TEXT,
    architectures_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    catalog_score INTEGER,
    icon_path TEXT,
    icon_mime_type TEXT,
    icon_sha256 TEXT,
    app_path TEXT NOT NULL UNIQUE
) STRICT;
CREATE INDEX idx_apps_name ON apps(name COLLATE NOCASE);
CREATE INDEX idx_apps_publisher ON apps(publisher COLLATE NOCASE);
CREATE VIRTUAL TABLE app_search USING fts5(
    id UNINDEXED, name, publisher, description, moniker, tags,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def build_app(conn: sqlite3.Connection, path: str, doc: dict[str, Any]) -> None:
    identifier = doc.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise BuildError(f"App sem id: {path}")
    package = doc.get("package") or {}
    if not isinstance(package, dict):
        raise BuildError(f"package inválido: {path}")
    tags = package.get("tags") or []
    architectures = doc.get("architectures") or []
    if not isinstance(tags, list) or not isinstance(architectures, list):
        raise BuildError(f"Arrays do app inválidos: {path}")
    versions = doc.get("versions")
    if not isinstance(versions, list) or not versions:
        raise BuildError(f"Lista de versões inválida: {path}")
    if doc.get("latestVersion") != versions[0].get("version"):
        raise BuildError(f"latestVersion não corresponde à primeira versão: {path}")
    media = doc.get("media") or {}
    icons = media.get("icons") or [] if isinstance(media, dict) else []
    icon = icons[0] if icons and isinstance(icons[0], dict) else {}
    catalog = doc.get("catalog") or {}
    score = catalog.get("score") if isinstance(catalog, dict) else None
    conn.execute("""INSERT INTO apps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (identifier, doc.get("source", "winget"), package.get("name"),
                  package.get("publisher"), package.get("description"),
                  package.get("longDescription"), package.get("homepage"),
                  package.get("packageUrl"), package.get("moniker"), package.get("locale"),
                  package.get("license"), doc.get("latestVersion"),
                  json_text(architectures), json_text(tags), score, icon.get("path"),
                  icon.get("mimeType"), icon.get("sha256"), path))
    conn.execute("INSERT INTO app_search VALUES (?, ?, ?, ?, ?, ?)",
                 (identifier, package.get("name") or "", package.get("publisher") or "",
                  package.get("description") or "", package.get("moniker") or "",
                  " ".join(str(tag) for tag in tags)))


def main() -> int:
    ref = git("rev-parse", "--verify", SOURCE_REF + "^{commit}").decode().strip()
    entries = list(tree_entries(ref))
    if not entries:
        raise BuildError(f"Nenhum app.json encontrado em {SOURCE_REF}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(OUTPUT.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN")
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("schemaVersion", "1"), ("sourceRef", SOURCE_REF),
            ("sourceCommit", ref), ("format", "WinProvision Catalog SQLite Index")
        ])
        product_count = version_count = 0
        for index, (path, raw) in enumerate(blob_stream(entries), start=1):
            try:
                doc = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BuildError(f"JSON inválido em {path}: {error}") from error
            if not isinstance(doc, dict):
                raise BuildError(f"JSON não é objeto: {path}")
            build_app(conn, path, doc)
            product_count += 1
            version_count += len(doc["versions"])
            if index % BATCH_SIZE == 0:
                print(f"Lidos {index:,}/{len(entries):,} JSON...", flush=True)
        conn.commit()
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("appCount", str(product_count)), ("versionCount", str(version_count)),
        ])
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BuildError("SQLite falhou na verificação de integridade")
        conn.close()
        temporary.replace(OUTPUT)
        size = OUTPUT.stat().st_size
        print(f"SQLite: {OUTPUT}")
        print(f"Origem: {SOURCE_REF} @ {ref}")
        print(f"Apps: {product_count:,}; versões catalogadas: {version_count:,}")
        print(f"Tamanho: {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")
        return 0
    except Exception:
        conn.close()
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, sqlite3.Error) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        raise SystemExit(1)

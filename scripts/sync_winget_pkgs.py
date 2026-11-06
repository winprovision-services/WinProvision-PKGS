#!/usr/bin/env python3
"""Incrementally project WinGet YAML manifest changes into catalog JSON."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ".sync/winget-state.json"
CATALOG_BRANCH = os.environ.get("CATALOG_BRANCH", "manifests")
UPSTREAM_REPOSITORY = os.environ.get("UPSTREAM_REPOSITORY", "microsoft/winget-pkgs")
UPSTREAM_REF = os.environ.get("UPSTREAM_REF", "master")
MAX_UPSTREAM_COMMITS_PER_RUN = int(os.environ.get("MAX_UPSTREAM_COMMITS_PER_RUN", "500"))
INITIAL_FETCH_DEPTH = 128
MAX_FETCH_DEPTH = 32768
MAX_CATALOG_PATH_BYTES = 4096
ALLOWED_ARCHITECTURES = {"arm", "arm64", "x64", "x86", "neutral"}
ALLOWED_INSTALLER_TYPES = {
    "appx", "burn", "exe", "font", "inno", "msi", "msix", "nullsoft",
    "portable", "pwa", "wix", "zip",
}
INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
GUID = re.compile(r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$")

LOCALE_KEY_MAP = {
    "PackageName": "name",
    "Publisher": "publisher",
    "PublisherUrl": "publisherUrl",
    "PublisherSupportUrl": "publisherSupportUrl",
    "PackageUrl": "packageUrl",
    "License": "license",
    "LicenseUrl": "licenseUrl",
    "Copyright": "copyright",
    "CopyrightUrl": "copyrightUrl",
    "ShortDescription": "description",
    "Description": "longDescription",
    "Moniker": "moniker",
    "Tags": "tags",
    "ReleaseNotes": "releaseNotes",
    "ReleaseNotesUrl": "releaseNotesUrl",
    "PurchaseUrl": "purchaseUrl",
    "InstallationNotes": "installationNotes",
    "Documentations": "documentations",
    "PackageLocale": "locale",
}

INSTALLER_KEY_MAP = {
    "Architecture": "architecture",
    "InstallerType": "type",
    "Scope": "scope",
    "InstallerUrl": "url",
    "InstallerSha256": "sha256",
    "InstallerLocale": "locale",
    "MinimumOSVersion": "minimumOSVersion",
    "InstallModes": "installModes",
    "InstallerSwitches": "installerSwitches",
    "UpgradeBehavior": "upgradeBehavior",
    "InstallerSuccessCodes": "successCodes",
    "Commands": "commands",
    "Protocols": "protocols",
    "FileExtensions": "fileExtensions",
    "AppsAndFeaturesEntries": "appsAndFeaturesEntries",
    "Dependencies": "dependencies",
    "PackageFamilyName": "packageFamilyName",
    "ProductCode": "productCode",
    "ElevationRequirement": "elevationRequirement",
    "Platform": "platform",
    "NestedInstallerType": "nestedInstallerType",
    "NestedInstallerFiles": "nestedInstallerFiles",
    "UnsupportedOSArchitectures": "unsupportedOSArchitectures",
    "UnsupportedArguments": "unsupportedArguments",
    "ExpectedReturnCodes": "expectedReturnCodes",
    "RepairBehavior": "repairBehavior",
    "InstallerAbortsTerminal": "installerAbortsTerminal",
    "InstallLocationRequired": "installLocationRequired",
    "DownloadCommandProhibited": "downloadCommandProhibited",
    "Authentication": "authentication",
}

PRIORITY = {
    "exe": 1, "inno": 1, "nullsoft": 1, "burn": 1,
    "wix": 2, "msi": 2,
    "msix": 3, "appx": 3,
    "portable": 4, "zip": 4,
}


class SyncError(RuntimeError):
    pass


def git(repo: Path, *args: str, input_bytes: bytes | None = None,
        check: bool = True, env: dict[str, str] | None = None) -> bytes:
    command = ["git", "-C", str(repo), *args]
    child_env = os.environ.copy()
    child_env["GIT_TERMINAL_PROMPT"] = "0"
    child_env["LC_ALL"] = "C"
    if env:
        child_env.update(env)
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=child_env)
    if check and result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise SyncError(f"Falha em {' '.join(command[:5])}: {message}")
    return result.stdout


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    git(repo, *args, env=env)


def log(message: str) -> None:
    print(message, flush=True)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value if item is not None]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_ref(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", ref).decode().strip()


def cat_files(repo: Path, commit: str, paths: Iterable[str]) -> dict[str, bytes]:
    """Read many tree paths in one cat-file process; missing paths are omitted."""
    unique = list(dict.fromkeys(paths))
    if not unique:
        return {}
    refs: list[str] = []
    for path in unique:
        if "\n" in path or "\r" in path or "\x00" in path:
            raise SyncError(f"Caminho upstream inválido: {path!r}")
        refs.append(f"{commit}:{path}")
    output = git(repo, "cat-file", "--batch", input_bytes=("\n".join(refs) + "\n").encode())
    cursor = 0
    contents: dict[str, bytes] = {}
    for path in unique:
        line_end = output.find(b"\n", cursor)
        if line_end < 0:
            raise SyncError("Resposta incompleta de git cat-file --batch")
        header = output[cursor:line_end].split()
        cursor = line_end + 1
        if len(header) == 2 and header[1] == b"missing":
            continue
        if len(header) != 3 or header[1] != b"blob":
            raise SyncError(f"Objeto Git não é blob para {path}")
        size = int(header[2])
        end = cursor + size
        if end >= len(output) or output[end:end + 1] != b"\n":
            raise SyncError(f"Conteúdo incompleto para {path}")
        contents[path] = output[cursor:end]
        cursor = end + 1
    return contents


def ls_tree_files(repo: Path, commit: str, folders: Iterable[str]) -> list[str]:
    folders = sorted(set(folders))
    found: list[str] = []
    # Keep the argument vector bounded for large catch-up batches.
    for offset in range(0, len(folders), 200):
        chunk = folders[offset:offset + 200]
        pathspecs = [f":(literal){folder}" for folder in chunk]
        raw = git(repo, "ls-tree", "-r", "--name-only", "-z", commit, "--", *pathspecs)
        found.extend(part.decode("utf-8", "strict") for part in raw.split(b"\0") if part)
    return sorted(set(path for path in found if path.endswith(".yaml")))


def parse_diff(raw: bytes) -> list[tuple[str, str]]:
    tokens = raw.split(b"\0")
    result: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(tokens) and tokens[i]:
        status = tokens[i].decode("ascii", "strict")
        path = tokens[i + 1].decode("utf-8", "strict")
        if status.startswith(("R", "C")):
            if i + 2 >= len(tokens):
                raise SyncError("Diff Git incompleto para rename/copy")
            raise SyncError("Diff retornou rename/copy apesar de --no-renames")
        result.append((status, path))
        i += 2
    return result


def validate_identifier(identifier: str) -> list[str]:
    if not isinstance(identifier, str) or not 2 <= len(identifier) <= 128:
        raise SyncError(f"PackageIdentifier inválido: {identifier!r}")
    segments = identifier.split(".")
    if not 2 <= len(segments) <= 8:
        raise SyncError(f"Quantidade de segmentos inválida no ID: {identifier}")
    for segment in segments:
        if not segment or len(segment) > 32 or segment in {".", ".."}:
            raise SyncError(f"Segmento inválido no ID: {identifier}")
        if segment != segment.strip() or INVALID_PATH_CHARS.search(segment):
            raise SyncError(f"Caractere inválido no ID: {identifier}")
    return segments


def product_dir(identifier: str) -> str:
    segments = validate_identifier(identifier)
    first = identifier[0].lower()
    partition = first if first in "0123456789abcdefghijklmnopqrstuvwxyz" else "_"
    encoded = [quote(segment, safe="-._~") for segment in segments]
    return "/".join([partition, *encoded])


def encoded_version(version: str) -> str:
    if not isinstance(version, str) or not version or len(version) > 128:
        raise SyncError(f"PackageVersion inválida: {version!r}")
    if version in {".", ".."} or INVALID_PATH_CHARS.search(version):
        raise SyncError(f"Caractere inválido na versão: {version!r}")
    return quote(version, safe="-._~")


def yaml_document(data: bytes, source_path: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(data)
    except yaml.YAMLError as error:
        raise SyncError(f"YAML inválido em {source_path}: {error}") from error
    if not isinstance(loaded, dict):
        raise SyncError(f"Manifesto YAML não é um objeto: {source_path}")
    identifier = loaded.get("PackageIdentifier")
    version = loaded.get("PackageVersion")
    if not isinstance(identifier, str) or not isinstance(version, str):
        raise SyncError(f"Manifesto sem PackageIdentifier/PackageVersion: {source_path}")
    validate_identifier(identifier)
    encoded_version(version)
    return loaded


def collect_changed_keys(upstream: Path, base: str, head: str,
                         changes: list[tuple[str, str]]) -> tuple[set[tuple[str, str]], dict[tuple[str, str], set[str]]]:
    yaml_paths = sorted({path for _, path in changes
                         if path.startswith("manifests/") and path.endswith(".yaml")})
    if not yaml_paths:
        return set(), {}
    base_docs = cat_files(upstream, base, yaml_paths)
    head_docs = cat_files(upstream, head, yaml_paths)
    affected: set[tuple[str, str]] = set()
    folders: dict[tuple[str, str], set[str]] = {}
    for path in yaml_paths:
        for data in (base_docs.get(path), head_docs.get(path)):
            if data is None:
                continue
            doc = yaml_document(data, path)
            key = (doc["PackageIdentifier"], doc["PackageVersion"])
            affected.add(key)
            folders.setdefault(key, set()).add(PurePosixPath(path).parent.as_posix())
    return affected, folders


def camel_key(key: str) -> str:
    if key in LOCALE_KEY_MAP:
        return LOCALE_KEY_MAP[key]
    return key[:1].lower() + key[1:] if key else key


def localization(doc: dict[str, Any]) -> dict[str, Any]:
    ignored = {
        "ManifestType", "ManifestVersion", "PackageIdentifier", "PackageVersion",
    }
    return {camel_key(key): clean(value) for key, value in doc.items()
            if key not in ignored and value is not None}


def default_silent(installer_type: str) -> str | None:
    return {
        "msi": "/quiet /norestart",
        "wix": "/quiet /norestart",
        "burn": "/quiet /norestart",
    }.get(installer_type)


def uninstall_entry(installer: dict[str, Any]) -> dict[str, str] | None:
    family = installer.get("PackageFamilyName")
    if isinstance(family, str) and family:
        return {"type": "packageFamilyName", "value": family}
    product_code = installer.get("ProductCode")
    if isinstance(product_code, str) and product_code:
        if GUID.fullmatch(product_code):
            return {"type": "productCode", "value": product_code}
        return {"type": "registryKey", "value": product_code}
    entries = installer.get("AppsAndFeaturesEntries") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        family = entry.get("PackageFamilyName")
        if isinstance(family, str) and family:
            return {"type": "packageFamilyName", "value": family}
        product_code = entry.get("ProductCode")
        if isinstance(product_code, str) and product_code:
            if GUID.fullmatch(product_code):
                return {"type": "productCode", "value": product_code}
            return {"type": "registryKey", "value": product_code}
    return None


def resolve_installer_type(raw: dict[str, Any]) -> str:
    value = raw.get("InstallerType")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()

    # InstallerType is omitted by some older or compact WinGet manifests.
    # Recover strong file-format signals from the URL; otherwise WinGet treats
    # an unspecified installer as a generic executable installer.
    url = raw.get("InstallerUrl")
    path = urlsplit(url).path.lower() if isinstance(url, str) else ""
    for suffix, inferred in (
        (".msixbundle", "msix"), (".appxbundle", "appx"),
        (".msix", "msix"), (".appx", "appx"),
        (".msi", "msi"), (".zip", "zip"),
    ):
        if path.endswith(suffix):
            return inferred
    return "exe"


def convert_installer(raw: dict[str, Any], seen_ids: dict[str, int]) -> dict[str, Any]:
    installer: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        mapped = INSTALLER_KEY_MAP.get(key, key[:1].lower() + key[1:])
        installer[mapped] = clean(value)

    architecture = str(raw.get("Architecture", "neutral")).lower()
    if architecture not in ALLOWED_ARCHITECTURES:
        raise SyncError(f"Arquitetura WinGet não suportada: {architecture}")
    installer["architecture"] = architecture
    resolved_installer_type = resolve_installer_type(raw)
    if resolved_installer_type not in ALLOWED_INSTALLER_TYPES:
        raise SyncError(f"InstallerType WinGet não suportado: {resolved_installer_type!r}")
    installer["type"] = resolved_installer_type
    url = raw.get("InstallerUrl")
    digest = raw.get("InstallerSha256")
    if not isinstance(url, str) or not re.match(r"^https?://", url, re.IGNORECASE):
        raise SyncError(f"InstallerUrl HTTP(S) ausente ou inválida: {url!r}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise SyncError(f"InstallerSha256 inválido: {digest!r}")
    installer["url"] = url
    installer["sha256"] = digest.lower()
    if "Scope" in raw:
        installer["scope"] = str(raw["Scope"]).lower()

    installer["priority"] = PRIORITY.get(resolved_installer_type, 5)
    base_parts = [architecture]
    scope = installer.get("scope")
    if scope:
        base_parts.append(scope)
    base_parts.append(resolved_installer_type)
    base_id = "-".join(base_parts)
    seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
    installer["id"] = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"

    installer["successCodes"] = clean(raw.get("InstallerSuccessCodes") or [])
    installer["dependencies"] = clean(raw.get("Dependencies") or [])
    switches = raw.get("InstallerSwitches") or {}
    silent = switches.get("Silent") or switches.get("SilentWithProgress")
    source = "manifest"
    if not silent:
        silent = default_silent(resolved_installer_type)
        source = "default"
    if silent:
        installer["silent"] = {"arguments": silent, "source": source}

    elevation = raw.get("ElevationRequirement")
    if elevation:
        installer["elevation"] = {"requirement": elevation, "source": "manifest"}
    elif scope == "machine":
        installer["elevation"] = {"requirement": "elevationRequired", "source": "inferred"}
    else:
        installer.pop("elevationRequirement", None)

    uninstall = uninstall_entry(raw)
    if uninstall:
        installer["uninstall"] = uninstall
    installer.pop("packageFamilyName", None)
    installer.pop("productCode", None)
    installer.pop("elevationRequirement", None)
    return installer


def commit_date_for_folder(upstream: Path, commit: str, folder: str) -> str:
    pathspec = f":(literal){folder}"
    raw = git(upstream, "log", "-1", "--format=%cI", commit, "--", pathspec).decode().strip()
    if not raw:
        raw = git(upstream, "show", "-s", "--format=%cI", commit).decode().strip()
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise SyncError(f"Data de commit upstream inválida: {raw}") from error


def build_version(upstream: Path, upstream_commit: str, folder: str,
                  files: dict[str, bytes], expected_id: str,
                  expected_version: str) -> tuple[dict[str, Any], bytes]:
    docs: list[tuple[str, dict[str, Any], bytes]] = []
    for path, raw in sorted(files.items()):
        doc = yaml_document(raw, path)
        if (doc["PackageIdentifier"], doc["PackageVersion"]) != (expected_id, expected_version):
            raise SyncError(f"IDs divergentes dentro da versão {expected_id} {expected_version}: {path}")
        docs.append((path, doc, raw))

    version_docs = [entry for entry in docs if entry[1].get("ManifestType", "").lower() == "version"]
    installer_docs = [entry for entry in docs if entry[1].get("ManifestType", "").lower() == "installer"]
    locale_docs = [entry for entry in docs if entry[1].get("ManifestType", "").lower() in {"locale", "defaultlocale"}]
    if len(version_docs) != 1 or len(installer_docs) != 1:
        raise SyncError(f"Conjunto de manifestos incompleto para {expected_id} {expected_version}: "
                        f"version={len(version_docs)}, installer={len(installer_docs)}")

    version_doc = version_docs[0][1]
    installer_doc = installer_docs[0][1]
    localizations: dict[str, Any] = {}
    for _, doc, _ in locale_docs:
        locale = doc.get("PackageLocale")
        if not isinstance(locale, str) or not locale:
            raise SyncError(f"PackageLocale inválido em {expected_id} {expected_version}")
        if locale in localizations:
            raise SyncError(f"PackageLocale duplicado ({locale}) em {expected_id} {expected_version}")
        localizations[locale] = localization(doc)

    default_locale = version_doc.get("DefaultLocale")
    if not isinstance(default_locale, str) or not default_locale:
        default_locale = sorted(localizations)[0] if localizations else None
    if default_locale and default_locale not in localizations:
        raise SyncError(f"DefaultLocale {default_locale} sem manifesto locale em {expected_id} {expected_version}")

    raw_installers = installer_doc.get("Installers")
    if not isinstance(raw_installers, list) or not raw_installers:
        raise SyncError(f"Nenhum instalador em {expected_id} {expected_version}")
    seen_ids: dict[str, int] = {}
    if any(not isinstance(item, dict) for item in raw_installers):
        raise SyncError(f"Entrada de instalador inválida em {expected_id} {expected_version}")
    inherited_installer_fields = {
        key: installer_doc[key] for key in INSTALLER_KEY_MAP if key in installer_doc
    }
    try:
        installers = [
            convert_installer({**inherited_installer_fields, **item}, seen_ids)
            for item in raw_installers
        ]
    except SyncError as error:
        raise SyncError(f"Manifesto installer {installer_docs[0][0]}: {error}") from error
    if not installers:
        raise SyncError(f"Nenhum instalador válido em {expected_id} {expected_version}")

    release_date = version_doc.get("PackageReleaseDate")
    release_date_source = "manifest" if release_date else "commit"
    if not release_date:
        release_date = commit_date_for_folder(upstream, upstream_commit, folder)
    elif isinstance(release_date, (dt.date, dt.datetime)):
        release_date = release_date.isoformat()
    else:
        release_date = str(release_date)

    source_manifests = [{"path": path, "sha256": sha256(raw)}
                        for path, _, raw in docs]
    default_package = localizations.get(default_locale, {}) if default_locale else {}
    detail: dict[str, Any] = {
        "schemaVersion": 1,
        "source": "winget",
        "id": expected_id,
        "version": expected_version,
        "installers": installers,
        "packageLocalizations": localizations,
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": upstream_commit,
            "manifestVersion": version_doc.get("ManifestVersion"),
            "manifests": source_manifests,
        },
    }
    if default_locale:
        detail["defaultLocale"] = default_locale
    if release_date:
        detail["releaseDate"] = release_date
        detail["releaseDateSource"] = release_date_source
    for key in ("licenseUrl", "publisherUrl", "publisherSupportUrl", "releaseNotesUrl"):
        if default_package.get(key) is not None:
            detail[key] = default_package[key]
    encoded = canonical_bytes(detail)
    return detail, encoded


def version_parts(value: str) -> list[tuple[int, bool, str]]:
    value = value.strip()
    digit = re.search(r"[0-9]", value)
    if digit and ("." not in value or digit.start() < value.index(".")):
        value = value[digit.start():]
    parts: list[tuple[int, bool, str]] = []
    for part in value.split("."):
        match = re.match(r"([0-9]*)(.*)", part)
        number = int(match.group(1) or 0)
        suffix = match.group(2).strip().casefold()
        if number > 2**64 - 1:
            number, suffix = 0, part.strip().casefold()
        parts.append((number, not bool(suffix), suffix))
    return parts


def compare_versions(left: str, right: str) -> int:
    lval, rval = left.strip().casefold(), right.strip().casefold()
    for special, rank in (("latest", 1), ("unknown", -1)):
        if lval == special or rval == special:
            return 0 if lval == rval else rank if lval == special else -rank
    for lpart, rpart in zip_longest(version_parts(left), version_parts(right),
                                    fillvalue=(0, True, "")):
        if lpart != rpart:
            return 1 if lpart > rpart else -1
    return 0


def zip_longest(left: Iterable[Any], right: Iterable[Any], fillvalue: Any):
    sentinel = object()
    li, ri = iter(left), iter(right)
    while True:
        lvalue, rvalue = next(li, sentinel), next(ri, sentinel)
        if lvalue is sentinel and rvalue is sentinel:
            return
        yield (fillvalue if lvalue is sentinel else lvalue,
               fillvalue if rvalue is sentinel else rvalue)


def sort_version_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def compare(a: dict[str, Any], b: dict[str, Any]) -> int:
        order = compare_versions(a["version"], b["version"])
        if order:
            return -order
        date_a, date_b = a.get("releaseDate") or "", b.get("releaseDate") or ""
        if date_a != date_b:
            return -1 if date_a > date_b else 1
        return -1 if a["version"] > b["version"] else 1 if a["version"] < b["version"] else 0
    return sorted(rows, key=functools.cmp_to_key(compare))


def read_target_file(target_repo: Path, commit: str, path: str) -> bytes | None:
    exists = git(target_repo, "ls-tree", "--name-only", "-z", commit, "--",
                 f":(literal){path}")
    if not exists:
        return None
    return git(target_repo, "show", f"{commit}:{path}")


def load_state(target_repo: Path, commit: str) -> dict[str, Any]:
    raw = read_target_file(target_repo, commit, STATE_PATH)
    if raw is None:
        raise SyncError(f"Estado de sincronização ausente em {CATALOG_BRANCH}:{STATE_PATH}")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SyncError(f"JSON de estado inválido: {error}") from error
    if state.get("schemaVersion") != 1:
        raise SyncError("Versão desconhecida do estado de sincronização")
    if state.get("upstreamRepository") != UPSTREAM_REPOSITORY or state.get("upstreamRef") != UPSTREAM_REF:
        raise SyncError("Estado aponta para outro repositório/ref upstream")
    commit_sha = state.get("upstreamCommit")
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise SyncError("SHA upstream inválido no arquivo de estado")
    return state


def fetch_upstream(source_repo: Path, baseline: str) -> tuple[str, list[str]]:
    upstream_url = f"https://github.com/{UPSTREAM_REPOSITORY}.git"
    run_git(source_repo, "remote", "add", "origin", upstream_url)
    git(source_repo, "config", "remote.origin.promisor", "true")
    git(source_repo, "config", "remote.origin.partialclonefilter", "blob:none")
    git(source_repo, "fetch", "--filter=blob:none", "--no-tags",
        f"--depth={INITIAL_FETCH_DEPTH}", "origin", f"refs/heads/{UPSTREAM_REF}")
    head = git_ref(source_repo, "FETCH_HEAD")

    depth = INITIAL_FETCH_DEPTH
    probe = subprocess.run(["git", "-C", str(source_repo), "merge-base", "--is-ancestor", baseline, head],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    attempts = 0
    while probe.returncode != 0 and depth < MAX_FETCH_DEPTH and attempts < 8:
        depth = min(depth * 2, MAX_FETCH_DEPTH)
        log(f"Aprofundando histórico upstream (até {depth} commits)...")
        git(source_repo, "fetch", "--filter=blob:none", "--no-tags", f"--deepen={depth}",
            "origin", f"refs/heads/{UPSTREAM_REF}")
        probe = subprocess.run(["git", "-C", str(source_repo), "merge-base", "--is-ancestor", baseline, head],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        attempts += 1
    if probe.returncode != 0:
        raise SyncError("Baseline upstream não é ancestral do ref atual ou ficou fora do histórico "
                        "parcial. Nenhum arquivo foi publicado; confira force-push ou rebaseline manual.")

    raw_commits = git(source_repo, "rev-list", "--first-parent", "--reverse",
                      "--ancestry-path", f"{baseline}..{head}")
    commits = [line.decode("ascii") for line in raw_commits.splitlines() if line]
    return head, commits


def update_index(target_repo: Path, index_file: Path,
                 writes: dict[str, bytes], deletes: set[str]) -> None:
    env = {"GIT_INDEX_FILE": str(index_file)}
    records = bytearray()
    zero_oid = b"0" * 40
    for path in sorted(deletes - writes.keys()):
        records.extend(b"0 " + zero_oid + b"\t" + path.encode("utf-8") + b"\0")
    for path, data in sorted(writes.items()):
        oid = git(target_repo, "hash-object", "-w", "--stdin", input_bytes=data).strip()
        if not re.fullmatch(rb"[0-9a-f]{40}", oid):
            raise SyncError(f"Git retornou blob SHA inválido para {path}")
        records.extend(b"100644 " + oid + b"\t" + path.encode("utf-8") + b"\0")
    if records:
        git(target_repo, "update-index", "-z", "--index-info", input_bytes=bytes(records), env=env)


def build_product_updates(target_repo: Path, target_head: str, upstream: Path,
                          upstream_commit: str, affected: set[tuple[str, str]],
                          folders_by_key: dict[tuple[str, str], set[str]],
                          files_by_folder: dict[str, list[str]],
                          upstream_files: dict[str, bytes]) -> tuple[dict[str, bytes], set[str]]:
    writes: dict[str, bytes] = {}
    deletes: set[str] = set()
    keys_by_id: dict[str, set[str]] = {}
    detail_data: dict[tuple[str, str], tuple[dict[str, Any], bytes, str]] = {}

    for identifier, version in sorted(affected):
        folders = folders_by_key.get((identifier, version), set())
        current_paths = sorted({path for folder in folders for path in files_by_folder.get(folder, [])})
        current_files = {path: upstream_files[path] for path in current_paths if path in upstream_files}
        detail_path = f"{product_dir(identifier)}/versions/{encoded_version(version)}/app.json"
        if not current_files:
            deletes.add(detail_path)
        else:
            current_folders = sorted({PurePosixPath(path).parent.as_posix()
                                      for path in current_paths if path in upstream_files})
            if len(current_folders) != 1:
                raise SyncError(f"A versão {identifier} {version} está em múltiplas pastas upstream: {current_folders}")
            folder = current_folders[0]
            detail, serialized = build_version(upstream, upstream_commit, folder,
                                               current_files, identifier, version)
            writes[detail_path] = serialized
            detail_data[(identifier, version)] = (detail, serialized, detail_path)
        keys_by_id.setdefault(identifier, set()).add(version)

    for identifier, changed_versions in sorted(keys_by_id.items()):
        directory = product_dir(identifier)
        app_path = f"{directory}/app.json"
        old_raw = read_target_file(target_repo, target_head, app_path)
        existing: dict[str, Any] = {}
        if old_raw is not None:
            try:
                existing = json.loads(old_raw)
            except json.JSONDecodeError as error:
                raise SyncError(f"JSON de produto inválido no catálogo: {app_path}") from error
            if existing.get("id") != identifier:
                raise SyncError(f"ID divergente em {app_path}")

        rows_by_version: dict[str, dict[str, Any]] = {}
        for row in existing.get("versions", []):
            if not isinstance(row, dict) or not isinstance(row.get("version"), str):
                raise SyncError(f"Índice de versões inválido em {app_path}")
            rows_by_version[row["version"]] = row
        for version in changed_versions:
            prior = rows_by_version.get(version)
            if prior and isinstance(prior.get("detailPath"), str):
                deletes.add(f"{directory}/{prior['detailPath']}")
            generated = detail_data.get((identifier, version))
            if generated is None:
                rows_by_version.pop(version, None)
                continue
            detail, serialized, detail_path = generated
            architectures = sorted({item["architecture"] for item in detail["installers"]})
            row: dict[str, Any] = {
                "version": version,
                "architectures": architectures,
                "detailPath": f"versions/{encoded_version(version)}/app.json",
                "detailSha256": sha256(serialized),
            }
            if detail.get("releaseDate"):
                row["releaseDate"] = detail["releaseDate"]
                row["releaseDateSource"] = detail.get("releaseDateSource", "commit")
            rows_by_version[version] = row

        ordered_rows = sort_version_rows(list(rows_by_version.values()))
        if not ordered_rows:
            if old_raw is not None:
                for row in existing.get("versions", []):
                    rel = row.get("detailPath")
                    if isinstance(rel, str):
                        deletes.add(f"{directory}/{rel}")
                deletes.add(app_path)
            continue

        latest_version = ordered_rows[0]["version"]
        latest_pair = (identifier, latest_version)
        latest_generated = detail_data.get(latest_pair)
        if latest_generated:
            latest_detail = latest_generated[0]
        else:
            latest_row = next((row for row in ordered_rows if row["version"] == latest_version), None)
            if latest_row is None:
                raise SyncError(f"Versão mais recente ausente no índice de {identifier}")
            prior_detail_path = f"{directory}/{latest_row['detailPath']}"
            prior_raw = read_target_file(target_repo, target_head, prior_detail_path)
            if prior_raw is None:
                raise SyncError(f"Detalhe referenciado não existe: {prior_detail_path}")
            try:
                latest_detail = json.loads(prior_raw)
            except json.JSONDecodeError as error:
                raise SyncError(f"JSON de versão inválido: {prior_detail_path}") from error

        localizations = latest_detail.get("packageLocalizations") or {}
        default_locale = latest_detail.get("defaultLocale")
        if default_locale not in localizations and localizations:
            default_locale = sorted(localizations)[0]
        source_package = dict(localizations.get(default_locale, {})) if default_locale else {}

        old_package = dict(existing.get("package") or {})
        previous_source_package: dict[str, Any] = {}
        old_latest_version = existing.get("latestVersion")
        if old_raw is not None and isinstance(old_latest_version, str):
            old_row = next((row for row in existing.get("versions", [])
                            if row.get("version") == old_latest_version), None)
            if old_row and isinstance(old_row.get("detailPath"), str):
                old_detail_path = f"{directory}/{old_row['detailPath']}"
                old_detail_raw = read_target_file(target_repo, target_head, old_detail_path)
                if old_detail_raw is not None:
                    try:
                        old_detail = json.loads(old_detail_raw)
                        old_locales = old_detail.get("packageLocalizations") or {}
                        old_locale = old_detail.get("defaultLocale")
                        previous_source_package = dict(old_locales.get(old_locale, {}))
                    except (json.JSONDecodeError, TypeError):
                        raise SyncError(f"Detalhe antigo inválido: {old_detail_path}")

        package = dict(old_package)
        # Replace values that still match the prior WinGet projection, but keep
        # divergent/extra values as catalog-owned enrichment (e.g. homepage).
        for key in set(previous_source_package) | set(source_package):
            if key in source_package:
                package[key] = source_package[key]
            elif key in previous_source_package and package.get(key) == previous_source_package[key]:
                package.pop(key, None)
        package["localizations"] = localizations
        if default_locale:
            package.setdefault("locale", default_locale)
        if not old_raw:
            package = {**source_package, "localizations": localizations}
            if default_locale:
                package.setdefault("locale", default_locale)

        architectures = sorted({arch for row in ordered_rows for arch in row.get("architectures", [])})
        app = dict(existing)
        app.update({
            "schemaVersion": 1,
            "source": "winget",
            "id": identifier,
            "package": package,
            "latestVersion": latest_version,
            "architectures": architectures,
            "media": existing.get("media", {"icons": [], "screenshots": []}),
            "versions": ordered_rows,
        })
        writes[app_path] = canonical_bytes(app)
    return writes, deletes


def read_manifest_files(upstream: Path, base: str, head: str,
                        changes: list[tuple[str, str]]) -> tuple[set[tuple[str, str]], dict[tuple[str, str], set[str]], dict[str, list[str]], dict[str, bytes]]:
    affected, folders_by_key = collect_changed_keys(upstream, base, head, changes)
    folders = sorted({folder for paths in folders_by_key.values() for folder in paths})
    current_paths = ls_tree_files(upstream, head, folders)
    current_blobs = cat_files(upstream, head, current_paths)
    files_by_folder: dict[str, list[str]] = {folder: [] for folder in folders}
    for path in current_paths:
        folder = PurePosixPath(path).parent.as_posix()
        if folder in files_by_folder:
            files_by_folder[folder].append(path)
    return affected, folders_by_key, files_by_folder, current_blobs


def publish(target_repo: Path, target_head: str, writes: dict[str, bytes],
            deletes: set[str], index_file: Path, state: dict[str, Any],
            next_upstream_commit: str) -> str | None:
    state["upstreamCommit"] = next_upstream_commit
    state_bytes = canonical_bytes(state)
    writes = dict(writes)
    writes[STATE_PATH] = state_bytes
    env = {"GIT_INDEX_FILE": str(index_file)}
    git(target_repo, "read-tree", target_head, env=env)
    update_index(target_repo, index_file, writes, deletes)
    tree = git(target_repo, "write-tree", env=env).decode().strip()
    parent_tree = git_ref(target_repo, f"{target_head}^{{tree}}")
    if tree == parent_tree:
        return None
    message = f"Sync WinGet manifests through {next_upstream_commit[:12]}"
    identity = {
        "GIT_AUTHOR_NAME": "github-actions[bot]",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "github-actions[bot]",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    }
    commit = git(target_repo, "commit-tree", tree, "-p", target_head,
                 "-m", message, env=identity).decode().strip()
    git(target_repo, "push", "origin", f"{commit}:refs/heads/{CATALOG_BRANCH}")
    return commit


def main() -> int:
    if MAX_UPSTREAM_COMMITS_PER_RUN < 1:
        raise SyncError("MAX_UPSTREAM_COMMITS_PER_RUN precisa ser positivo")
    index_path = Path(tempfile.gettempdir()) / f"winget-manifests-{os.getpid()}.index"
    try:
        # This workflow runs from main. Fetch manifests as tree metadata only;
        # blobs are fetched lazily for the small set of touched products.
        git(ROOT, "config", "remote.origin.promisor", "true")
        git(ROOT, "config", "remote.origin.partialclonefilter", "blob:none")
        git(ROOT, "fetch", "--filter=blob:none", "--no-tags", "--depth=1", "origin",
            f"refs/heads/{CATALOG_BRANCH}:refs/remotes/origin/{CATALOG_BRANCH}")
        target_head = git_ref(ROOT, f"refs/remotes/origin/{CATALOG_BRANCH}")
        state = load_state(ROOT, target_head)
        baseline = state["upstreamCommit"]

        with tempfile.TemporaryDirectory(prefix="winget-pkgs-upstream-") as temp:
            upstream = Path(temp) / "upstream.git"
            run_git(upstream.parent, "init", "--bare", str(upstream))
            upstream_head, commits = fetch_upstream(upstream, baseline)
            if not commits:
                log(f"Sem novos commits upstream. Baseline permanece em {baseline[:12]}.")
                return 0

            batch_commits = commits[:MAX_UPSTREAM_COMMITS_PER_RUN]
            batch_head = batch_commits[-1]
            more_pending = len(commits) > len(batch_commits)
            log(f"Diff upstream: {baseline[:12]} -> {batch_head[:12]} "
                f"({len(batch_commits)} commits; head atual {upstream_head[:12]})")

            diff = git(upstream, "diff", "--name-status", "--no-renames", "-z",
                       baseline, batch_head, "--", "manifests")
            changes = parse_diff(diff)
            if not changes:
                log("Nenhum arquivo de manifesto mudou neste lote.")
                writes, deletes = {}, set()
            else:
                affected, folders, files_by_folder, current_blobs = read_manifest_files(
                    upstream, baseline, batch_head, changes)
                log(f"{len(changes)} arquivos upstream alterados; "
                    f"{len(affected)} versões afetadas.")
                writes, deletes = build_product_updates(
                    ROOT, target_head, upstream, batch_head, affected, folders,
                    files_by_folder, current_blobs)
                log(f"{sum(path.endswith('/app.json') and '/versions/' in path for path in writes)} "
                    f"detalhes e {sum(path.endswith('/app.json') and '/versions/' not in path for path in writes)} "
                    "índices de produto regenerados.")

            commit = publish(ROOT, target_head, writes, deletes, index_path,
                             state, batch_head)
            if commit:
                log(f"Publicado commit {commit} na branch {CATALOG_BRANCH}.")
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                        output.write(f"catalog_commit={commit}\n")
            else:
                log("Conteúdo já estava sincronizado; nenhum commit criado.")
            if more_pending:
                log(f"Há commits upstream pendentes; o próximo ciclo continuará em {batch_head[:12]}.")
            else:
                log("Catálogo alcançou o head upstream observado.")
        return 0
    finally:
        try:
            index_path.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"ERRO: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)

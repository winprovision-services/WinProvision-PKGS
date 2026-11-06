import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_winget_pkgs as sync  # noqa: E402


class InstallerProjectionTests(unittest.TestCase):
    def test_yaml_document_preserves_unquoted_win_get_version_text(self):
        document = sync.yaml_document(
            b"PackageIdentifier: JonathanMarler.msvcup\nPackageVersion: 2026_11_02\n",
            "manifest.yaml",
        )
        self.assertEqual(document["PackageVersion"], "2026_11_02")

    def test_cat_files_skips_non_blob_objects_without_desynchronizing_batch(self):
        link_oid = "a" * 40
        file_oid = "b" * 40
        tree = (f"160000 commit {link_oid}\tmanifests/a/link.yaml\0"
                f"100644 blob {file_oid}\tmanifests/b/file with spaces.yaml\0").encode()
        content = f"{file_oid} blob 3\nxyz\n".encode()
        with patch.object(sync, "git", side_effect=[tree, content]) as mocked_git:
            files = sync.cat_files(Path("."), "c" * 40,
                                   ["manifests/a/link.yaml", "manifests/b/file with spaces.yaml"])
        self.assertEqual(files, {"manifests/b/file with spaces.yaml": b"xyz"})
        self.assertEqual(mocked_git.call_args_list[1].kwargs["input_bytes"], f"{file_oid}\n".encode())

    def test_inherits_root_installer_type_and_allows_item_override(self):
        installer = sync.convert_installer({
            "InstallerType": "msi",
            "Architecture": "x64",
            "InstallerUrl": "https://example.test/package.msi",
            "InstallerSha256": "a" * 64,
        }, {})
        self.assertEqual(installer["type"], "msi")

        overridden = sync.convert_installer({
            "InstallerType": "exe",
            "Architecture": "x64",
            "InstallerUrl": "https://example.test/setup.exe",
            "InstallerSha256": "b" * 64,
        }, {})
        self.assertEqual(overridden["type"], "exe")

    def test_infers_known_installer_types_when_optional_field_is_missing(self):
        cases = {
            "https://example.test/package.msi?download=1": "msi",
            "https://example.test/package.msixbundle": "msix",
            "https://example.test/package.appxbundle": "appx",
            "https://example.test/package.zip": "zip",
            "https://example.test/download?id=42": "exe",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sync.resolve_installer_type({"InstallerUrl": url}), expected)

    def test_build_version_uses_manifest_level_installer_defaults(self):
        files = {
            "Contoso.App.yaml": b"""ManifestType: version
ManifestVersion: 1.9.0
PackageIdentifier: Contoso.App
PackageVersion: 1.0.0
PackageReleaseDate: '2026-11-01'
""",
            "Contoso.App.installer.yaml": b"""ManifestType: installer
ManifestVersion: 1.9.0
PackageIdentifier: Contoso.App
PackageVersion: 1.0.0
InstallerType: msi
Installers:
  - Architecture: x64
    InstallerUrl: https://example.test/package.msi
    InstallerSha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
        }
        detail, _ = sync.build_version(
            Path("."), "a" * 40, "manifests/c/Contoso/App/1.0.0",
            files, "Contoso.App", "1.0.0",
        )
        self.assertEqual(detail["installers"][0]["type"], "msi")
        self.assertEqual(detail["installers"][0]["id"], "x64-msi")

    def test_rejects_explicit_unsupported_type(self):
        with self.assertRaisesRegex(sync.SyncError, "InstallerType WinGet não suportado"):
            sync.convert_installer({
                "InstallerType": "unknown",
                "Architecture": "x64",
                "InstallerUrl": "https://example.test/setup.exe",
                "InstallerSha256": "a" * 64,
            }, {})


if __name__ == "__main__":
    unittest.main()

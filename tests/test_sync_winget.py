import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_winget_pkgs as sync  # noqa: E402


class InstallerProjectionTests(unittest.TestCase):
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

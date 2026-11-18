import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_latest_and_series_ignore_release_candidates(self):
        with patch.object(release, "fetch", return_value=b'<a href="releases/25.12.2/targets/">stable</a><a href="releases/24.10.6/targets/">old</a>'):
            self.assertEqual(release.resolve_version("https://example.org", "latest"), "25.12.2")
        with patch.object(release, "fetch", return_value=b'<a href="25.12.9/">9</a><a href="25.12.10/">10</a><a href="25.12.11-rc1/">rc</a>'):
            self.assertEqual(release.resolve_version("https://example.org", "openwrt-25.12"), "25.12.10")

    def test_matrix_uses_metadata_and_both_archive_formats(self):
        def fetch(url):
            if url.endswith("profiles.json"):
                return json.dumps({"profiles": {"generic": {}, "widora_mangopi-m28c": {}},
                                   "default_packages": ["apk-openssl"], "arch_packages": "aarch64_generic"}).encode()
            return (("a" * 64 + " *immortalwrt-sdk-25.12.2-test_gcc-99.Linux-x86_64.tar.zst\n") +
                    ("b" * 64 + "  immortalwrt-imagebuilder-25.12.2-test.Linux-x86_64.tar.xz\n")).encode()
        with patch.object(release, "fetch", side_effect=fetch):
            result = release.resolve("immortalwrt", "25.12.2", ["amd64", "m28c"])
        self.assertEqual([item["target"] for item in result["include"]], ["x86/64", "rockchip/armv8"])
        self.assertEqual(result["include"][1]["package_manager"], "apk")
        self.assertEqual(result["include"][1]["profile"], "widora_mangopi-m28c")

    def test_missing_device_profile_fails(self):
        with patch.object(release, "fetch", return_value=b'{"profiles":{"generic":{}}}'):
            with self.assertRaisesRegex(ValueError, "M28C requires ImmortalWrt"):
                release.resolve("openwrt", "25.12.6", ["m28c"])

    def test_snapshot_aliases_and_invalid_versions(self):
        for alias in ("SNAPSHOT", "main", "master"):
            self.assertEqual(release.resolve_version("https://example.org", alias), "SNAPSHOT")
        with self.assertRaises(ValueError):
            release.resolve_version("https://example.org", "$(invalid)")

    def test_corrupt_download_is_rejected(self):
        import io
        metadata = {"directory": "https://example.org/", "sdk": {"name": "sdk.tar.zst", "sha256": hashlib.sha256(b"valid").hexdigest()}}
        with tempfile.TemporaryDirectory() as directory, patch.object(release, "urlopen", return_value=io.BytesIO(b"corrupt")):
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                release.download(metadata, "sdk", Path(directory))
            self.assertFalse((Path(directory) / "sdk.tar.zst").exists())


if __name__ == "__main__":
    unittest.main()

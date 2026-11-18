import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prebuilt", ROOT / "scripts/fetch-prebuilt.py")
prebuilt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prebuilt)


class PrebuiltTests(unittest.TestCase):
    def test_only_proxy_groups_bypass_sdk_and_make(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/build-packages.sh", scripts)
            marker = root / "called.json"
            (scripts / "fetch-prebuilt.py").write_text(
                "import json, pathlib, sys\npathlib.Path(" + repr(str(marker)) + ").write_text(json.dumps(sys.argv[1:]))\n")
            metadata = root / "metadata.json"
            metadata.write_text(json.dumps({"id": "amd64", "package_manager": "apk"}))
            for group in ("openclash", "passwall"):
                subprocess.run(["bash", str(scripts / "build-packages.sh"), str(metadata), group], check=True, capture_output=True)
                args = json.loads(marker.read_text())
                self.assertEqual(args[1], group)
                self.assertEqual(args[2], str(root / "parts/amd64" / group))
                marker.unlink()
            result = subprocess.run(["bash", str(scripts / "build-packages.sh"), str(metadata), "mosdns"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)  # This group still requires an SDK.
            self.assertFalse(marker.exists())

    def test_repositories_keep_release_urls_and_opkg_signature_check(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = Path(directory)
            base = "https://downloads.immortalwrt.org/releases/25.12.2/packages/aarch64_generic/"
            (builder / "repositories").write_text("\n".join(base + feed + "/packages.adb" for feed in ("base", "luci", "packages", "routing")))
            self.assertEqual(prebuilt.repositories(builder, "apk").splitlines(),
                             [base + "luci/packages.adb", base + "packages/packages.adb"])
            (builder / "repositories.conf").write_text("src/gz official " + base + "luci\nsrc local file:packages\n")
            self.assertEqual(prebuilt.repositories(builder, "opkg").splitlines(),
                             ["src/gz official " + base + "luci", "option check_signature 1"])

    def test_inventory_rejects_missing_extra_and_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            package = dest / "xray-core-1.0-r1.apk"
            package.write_bytes(b"package")
            entries = prebuilt.inventory(dest, ["xray-core"], "apk", "x86_64")
            self.assertEqual(entries[0]["sha256"], hashlib.sha256(b"package").hexdigest())
            with self.assertRaisesRegex(ValueError, "no prebuilt packages"):
                prebuilt.inventory(dest, ["xray-core", "luci-app-passwall"], "apk", "x86_64")
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                prebuilt.inventory(dest, ["luci-app-passwall"], "apk", "x86_64")
            package.unlink()
            (dest / "xray-core_1.0-r1_aarch64_generic.ipk").write_bytes(b"wrong architecture")
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                prebuilt.inventory(dest, ["xray-core"], "opkg", "x86_64")

    def test_opkg_uses_target_architecture_and_official_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(prebuilt.subprocess, "run") as run:
            root = Path(directory)
            builder = root / "builder"
            builder.mkdir()
            (builder / "repositories.conf").write_text("src/gz luci https://downloads.immortalwrt.org/releases/24.10.6/packages/x86_64/luci\n")
            dest, state = root / "dest", root / "state"
            dest.mkdir()
            state.mkdir()
            prebuilt.fetch_packages({"package_manager": "opkg", "arch": "x86_64"}, builder, dest, ["luci-app-openclash"], state)
            update, download = run.call_args_list
            self.assertEqual(update.args[0][-1], "update")
            self.assertEqual(download.args[0][-2:], ["download", "luci-app-openclash"])
            self.assertIn("x86_64:200", update.args[0])
            self.assertEqual(update.kwargs["env"]["OPKG_KEYS"], str(builder / "keys"))
            self.assertIn("option check_signature 1", (state / "repositories.conf").read_text())


@unittest.skipUnless(os.getenv("TEST_APK_SDK"), "Set TEST_APK_SDK to test native signed prebuilt downloads")
class NativePrebuiltTests(unittest.TestCase):
    def test_signed_index_fetches_only_requested_packages_and_rejects_tampering(self):
        native = Path(os.environ["TEST_APK_SDK"]).resolve()
        apk = str(native / "staging_dir/host/bin/apk")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder, repo, output = [root / name for name in ("builder", "repo", "output")]
            for path in (builder, repo, output):
                path.mkdir()
            (builder / "staging_dir").symlink_to(native / "staging_dir", target_is_directory=True)
            keys = builder / "keys"
            keys.mkdir()
            private = root / "private.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(private)], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(keys / "feed.pem")], check=True, capture_output=True)
            for name in ("luci-app-passwall", "dependency"):
                payload = root / name
                payload.mkdir()
                (payload / "data").write_text(name)
                command = [apk, "mkpkg", "--info", "name:" + name, "--info", "version:1.0-r1", "--info", "arch:aarch64_generic", "--info", "license:MIT", "--files", str(payload), "--output", str(repo / (name + "-1.0-r1.apk"))]
                if name == "luci-app-passwall":
                    command += ["--info", "depends:dependency"]
                subprocess.run(command, check=True, capture_output=True)
            subprocess.run([apk, "mkndx", "--allow-untrusted", "--sign", str(private), "--output", str(repo / "packages.adb"), *map(str, repo.glob("*.apk"))], check=True, capture_output=True)
            for case in ("valid", "tampered"):
                if case == "tampered":
                    target = repo / "luci-app-passwall-1.0-r1.apk"
                    target.write_bytes(target.read_bytes()[:50])
                state, dest = root / case, output / case
                state.mkdir()
                dest.mkdir()
                with patch.object(prebuilt, "repositories", return_value=str(repo / "packages.adb") + "\n"):
                    if case == "tampered":
                        with self.assertRaises(subprocess.CalledProcessError):
                            prebuilt.fetch_packages({"package_manager": "apk", "arch": "aarch64_generic"}, builder, dest, ["luci-app-passwall"], state)
                    else:
                        prebuilt.fetch_packages({"package_manager": "apk", "arch": "aarch64_generic"}, builder, dest, ["luci-app-passwall"], state)
                        self.assertEqual([p.name for p in dest.glob("*.apk")], ["luci-app-passwall-1.0-r1.apk"])


if __name__ == "__main__":
    unittest.main()

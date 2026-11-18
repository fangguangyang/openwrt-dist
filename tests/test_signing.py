import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("signing_key", Path(__file__).resolve().parents[1] / "scripts/signing-key.py")
signing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signing)


class SigningTests(unittest.TestCase):
    def test_cli_keeps_private_key_in_build_and_places_public_key_at_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = root / "scripts/signing-key.py"
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/signing-key.py", script)
            subprocess.run([sys.executable, str(script), "generate", "apk"], cwd=root,
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue((root / "apk-public-key.pem").is_file())
            self.assertTrue((root / ".build/signing/apk-signing-key.pem").is_file())
            self.assertFalse((root / ".build/signing/apk-public-key.pem").exists())

    def test_missing_signing_secret_is_rejected(self):
        for manager, name in signing.SECRET_NAMES.items():
            with self.assertRaisesRegex(ValueError, name):
                signing.signing_material(manager, {})

    def test_opkg_requires_both_halves_of_the_keypair(self):
        for invalid in ['{}', '{"private_key":"secret"}', 'not json']:
            with self.assertRaisesRegex(ValueError, "JSON keypair"):
                signing.signing_material("opkg", {"OPKG_SIGNING_KEY": invalid})

    def test_apk_rebuild_installs_the_same_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = signing.generate("apk", root / "keys", "usign")
            first, second = root / "first-sdk", root / "second-sdk"
            first.mkdir()
            second.mkdir()
            environment = {"APK_SIGNING_KEY": secret.read_text()}
            signing.install("apk", first, environment)
            signing.install("apk", second, environment)
            self.assertEqual((first / "public-key.pem").read_bytes(), (second / "public-key.pem").read_bytes())
            self.assertEqual((first / "private-key.pem").stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "already exist"):
                signing.generate("apk", root / "keys", "usign")

    @unittest.skipUnless(os.getenv("TEST_APK_SDK"), "Set TEST_APK_SDK to test native usign keypairs")
    def test_opkg_keypair_survives_repeated_installation(self):
        native = Path(os.environ["TEST_APK_SDK"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = signing.generate("opkg", root / "keys", str(native / "staging_dir/host/bin/usign"))
            public_keys = []
            for name in ("first-sdk", "second-sdk"):
                sdk = root / name
                sdk.mkdir()
                (sdk / "staging_dir").symlink_to(native / "staging_dir", target_is_directory=True)
                signing.install("opkg", sdk, {"OPKG_SIGNING_KEY": secret.read_text()})
                public_keys.append((sdk / "key-build.pub").read_bytes())
            self.assertEqual(public_keys[0], public_keys[1])


if __name__ == "__main__":
    unittest.main()

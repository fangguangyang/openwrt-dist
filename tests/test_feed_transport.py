"""Optional native apk transport smoke test: TEST_APK_SDK=/path/to/sdk."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("feed_assets", ROOT / "scripts/feed-assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)
spec = importlib.util.spec_from_file_location("signing_key", ROOT / "scripts/signing-key.py")
signing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signing)


@unittest.skipUnless(os.getenv("TEST_APK_SDK"), "Set TEST_APK_SDK to run the native signed-feed transport test")
class FeedTransportTests(unittest.TestCase):
    def test_signed_index_and_dependency_fetch_through_release_redirects(self):
        original_sdk = Path(os.environ["TEST_APK_SDK"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk, repo, keys = [root / name for name in ("sdk", "repository", "keys")]
            for path in (sdk, repo, keys):
                path.mkdir()
            (sdk / "staging_dir").symlink_to(original_sdk / "staging_dir", target_is_directory=True)
            (sdk / "scripts").symlink_to(original_sdk / "scripts", target_is_directory=True)
            apk = str(sdk / "staging_dir/host/bin/apk")
            secret = signing.generate("apk", root / "signing", "usign")
            signing.install("apk", sdk, {"APK_SIGNING_KEY": secret.read_text()})
            shutil.copy2(sdk / "public-key.pem", keys / "feed.pem")
            (repo / "build.json").write_text(json.dumps({"arch": "aarch64_generic"}))
            for name, dependency in [("feed-lib", None), ("feed-app", "feed-lib")]:
                payload = root / name
                payload.mkdir()
                (payload / (name + ".txt")).write_text(name)
                command = [apk, "mkpkg", "--info", "name:" + name, "--info", "version:1.0~abc-r1",
                           "--info", "arch:aarch64_generic", "--info", "license:MIT", "--files", str(payload),
                           "--output", str(repo / (name + "-1.0~abc-r1.apk"))]
                if dependency:
                    command += ["--info", "depends:" + dependency]
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run([apk, "mkndx", "--allow-untrusted", "--sign", str(sdk / "private-key.pem"),
                            "--output", str(repo / "packages.adb"), *[str(p) for p in repo.glob("*.apk")]], check=True)
            assets.normalize_apk_names(repo, sdk)
            subprocess.run([apk, "--keys-dir", str(keys), "verify", str(repo / "packages.adb")], check=True)
            self.assertTrue(all("~" not in p.name for p in repo.glob("*.apk")))
            requests = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    path = unquote(urlsplit(self.path).path)
                    requests.append(path)
                    name = path.rsplit("/", 1)[-1]
                    if path.startswith("/owner/repo/releases/download/tag/"):
                        # Redirect to an unrelated CDN-like path, including the
                        # index, to catch accidental changes of the feed base.
                        self.send_response(302)
                        self.send_header("Location", "/cdn/assets/" + name)
                        self.end_headers()
                    elif path.startswith("/cdn/assets/") and (repo / name).is_file():
                        data = (repo / name).read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self.send_error(404)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                downloads = root / "downloads"
                downloads.mkdir()
                apkroot = root / "apkroot"
                apkroot.mkdir()
                url = f"http://127.0.0.1:{server.server_port}/owner/repo/releases/download/tag/packages.adb"
                command = [apk, "--root", str(apkroot), "--arch", "aarch64_generic", "--keys-dir", str(keys),
                           "--repositories-file", "/dev/null", "--repository", url, "--no-cache",
                           "fetch", "--recursive", "--output", str(downloads), "feed-app"]
                environment = {k: v for k, v in os.environ.items() if not k.lower().endswith("proxy")}
                environment["NO_PROXY"] = "127.0.0.1,localhost"
                result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, env=environment)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(len(list(downloads.glob("*.apk"))), 2, result.stdout + repr(requests))
                package_requests = [p for p in requests if p.endswith(".apk") and "/releases/download/" in p]
                self.assertEqual(len(package_requests), 2, requests)
                self.assertTrue(all("~" not in p for p in package_requests))
                install = command[:command.index("fetch")] + ["add", "--initdb", "--usermode", "--no-scripts", "feed-app"]
                result = subprocess.run(install, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, env=environment)
                self.assertEqual(result.returncode, 0, result.stdout)
                for name in ("feed-app", "feed-lib"):
                    self.assertTrue((apkroot / (name + ".txt")).is_file(), result.stdout)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()

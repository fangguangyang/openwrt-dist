import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


publisher = module("publish_release", ROOT / "scripts/publish-release.py")
assets = module("feed_assets", ROOT / "scripts/feed-assets.py")


class FakeGitHub:
    def __init__(self, metadata, corrupt=None):
        self.release = None
        self.assets = []
        self.calls = []
        self.corrupt = corrupt
        self.next_id = 1
        self.url = publisher.release_location(metadata, "owner/repo")[1]
        self.draft_url = self.url.rsplit("/", 1)[0] + "/untagged-0123456789abcdef"
        self.uploaded_urls = []
        self.other_releases = []

    def api(self, endpoint, method="GET", payload=None):
        self.calls.append((method, endpoint, payload))
        if "/releases/tags/" in endpoint:
            if self.release is None or self.release.get("draft", False):
                raise publisher.GitHubAPIError(404)
            return dict(self.release)
        if method == "POST":
            if self.release is not None:
                raise AssertionError("Created another release instead of resuming the existing draft")
            self.release = dict(payload, id=5)
            return dict(self.release)
        if method == "DELETE":
            asset_id = int(endpoint.rsplit("/", 1)[1])
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]
            return None
        if method == "GET":
            page = int(endpoint.rsplit("=", 1)[1])
            if "/releases?" in endpoint:
                releases = self.other_releases + ([] if self.release is None else [dict(self.release)])
                return releases[(page - 1) * 100:page * 100]
            if f"/releases/{self.release['id']}/assets?" not in endpoint:
                raise AssertionError("Read assets from a different Release ID")
            return self.assets[(page - 1) * 100:page * 100]
        if not endpoint.endswith(f"/releases/{self.release['id']}"):
            raise AssertionError("Updated a different Release ID")
        was_draft = self.release.get("draft", False)
        self.release.update(payload)
        if was_draft and not self.release.get("draft", False):
            for asset in self.assets:
                if asset["browser_download_url"].startswith(self.draft_url + "/"):
                    asset["browser_download_url"] = self.url + "/" + asset["name"]
        return dict(self.release)

    def upload(self, files, clobber=False):
        self.calls.append(("UPLOAD", [path.name for path in files], clobber))
        for path in files:
            existing = next((asset for asset in self.assets if asset["name"] == path.name), None)
            if existing:
                if not clobber:
                    raise AssertionError("Attempted to overwrite an existing package")
                self.assets.remove(existing)
            url = (self.draft_url if self.release.get("draft", False) else self.url) + "/" + path.name
            self.uploaded_urls.append(url)
            self.assets.append(dict(id=self.next_id, name=path.name, state="uploaded", size=path.stat().st_size,
                                    browser_download_url=url, digest="sha256:" + publisher.sha256(path)))
            self.next_id += 1
        if self.corrupt and any(path.name == "packages.adb" for path in files):
            self.corrupt(self.assets)

    def api_upload(self, command, **kwargs):
        if command[:4] != ["gh", "api", "--method", "POST"]:
            raise AssertionError("Upload must use the release asset API")
        prefix = f"https://uploads.github.com/repos/owner/repo/releases/{self.release['id']}/assets?name="
        if not command[4].startswith(prefix):
            raise AssertionError("Uploaded to a different Release ID")
        if "Content-Type: application/octet-stream" not in command:
            raise AssertionError("Upload must send the original binary file")
        path = Path(command[command.index("--input") + 1])
        if command[4] != prefix + path.name:
            raise AssertionError("Uploaded filename differs from the original file")
        self.upload([path])
        return subprocess.CompletedProcess(command, 0)


class PublishTests(unittest.TestCase):
    def metadata(self, **changes):
        return dict(dict(id="m28c", distro="immortalwrt", version="25.12.2", target="rockchip/armv8",
                         package_manager="apk", arch="aarch64_generic", version_code="r1"), **changes)

    def feed(self, root, count=1, manager="apk"):
        required = ["packages.adb", "feed-key.pem"] if manager == "apk" else ["Packages", "Packages.gz", "Packages.sig", "feed-key.pub"]
        for name in [*required, "sources.json"]:
            (root / name).write_text("public test data")
        for number in range(count):
            (root / f"package-{number}.{'apk' if manager == 'apk' else 'ipk'}").write_bytes(b"package data")

    def test_release_url_contains_only_distribution_version_and_target(self):
        tag, url = publisher.release_location(self.metadata(), "owner/repo")
        self.assertEqual(tag, "packages-immortalwrt-25.12.2-rockchip-armv8")
        self.assertEqual(url, "https://github.com/owner/repo/releases/download/" + tag)
        for changes in [dict(version="SNAPSHOT"), dict(target="x86/64")]:
            self.assertNotEqual(tag, publisher.release_location(self.metadata(**changes), "owner/repo")[0])

    def test_assets_include_packages_signed_index_key_and_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            (root / "build-configs").mkdir()
            (root / "build-configs/platform.config").write_text("CONFIG_PACKAGE_example=m")
            tag, url = publisher.release_location(self.metadata(), "owner/repo")
            files = publisher.prepare_assets(root, self.metadata(), tag, url)
            self.assertTrue({"package-0.apk", "packages.adb", "feed-key.pem", "build-configs.tar.gz", "sha256sums"} <= {p.name for p in files})
            self.assertEqual(json.loads((root / "build.json").read_text())["feed_url"], url)

    def test_missing_indexes_and_private_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            (root / "packages.adb").unlink()
            with self.assertRaisesRegex(ValueError, "Missing feed asset"):
                publisher.prepare_assets(root, self.metadata(), "tag", "url")
            (root / "packages.adb").write_bytes(b"index")
            (root / "private-key.pem").write_text("-----BEGIN EC PRIVATE KEY-----")
            with self.assertRaisesRegex(ValueError, "private key"):
                publisher.prepare_assets(root, self.metadata(), "tag", "url")

    def test_opkg_special_versions_keep_bytes_but_get_safe_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "sample_1.0+git~abc_all.ipk"
            original.write_bytes(b"signed package bytes")
            assets.normalize_ipk_names(root)
            result = list(root.glob("*.ipk"))
            self.assertEqual(len(result), 1)
            self.assertNotIn("+", result[0].name)
            self.assertNotIn("~", result[0].name)
            self.assertEqual(result[0].read_bytes(), b"signed package bytes")

    def exercise_publish(self, root, corrupt=None):
        remote = FakeGitHub(self.metadata(), corrupt)
        return remote.calls, lambda: publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)

    def test_publish_waits_for_all_assets_and_handles_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root, count=105)
            calls, publish = self.exercise_publish(root)
            url = publish()
            self.assertIn("/releases/download/", url)
            self.assertTrue(next(payload for method, _, payload in calls if method == "POST")["draft"])
            self.assertTrue(any(method == "GET" and "page=2" in endpoint for method, endpoint, _ in calls))
            published = next(i for i, (method, _, payload) in enumerate(calls) if method == "PATCH" and payload.get("draft") is False)
            self.assertTrue(any(method == "GET" for method, _, _ in calls[published + 1:]))

    def test_draft_urls_become_final_urls_after_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            (root / "build-configs").mkdir()
            (root / "build-configs/platform.config").write_text("CONFIG_PACKAGE_example=m")
            remote = FakeGitHub(self.metadata())
            result = publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            self.assertTrue(any("/untagged-" in url and url.endswith("/build-configs.tar.gz") for url in remote.uploaded_urls))
            self.assertFalse(remote.release["draft"])
            self.assertTrue(all(asset["browser_download_url"] == result + "/" + asset["name"] for asset in remote.assets))

    def test_retry_resumes_draft_and_skips_already_uploaded_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root, count=3)
            remote = FakeGitHub(self.metadata())
            def interrupted_upload(files, clobber=False):
                remote.upload(files[:1], clobber)
                raise RuntimeError("Upload interrupted")
            with self.assertRaisesRegex(RuntimeError, "Upload interrupted"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, interrupted_upload)
            self.assertTrue(remote.release["draft"])
            original_package_id = remote.assets[0]["id"]
            start = len(remote.calls)
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "2", remote.api, remote.upload)
            self.assertFalse(remote.release["draft"])
            self.assertFalse(any(method == "POST" for method, _, _ in remote.calls[start:]))
            self.assertFalse(any(method == "UPLOAD" and "package-0.apk" in names for method, names, _ in remote.calls[start:]))
            self.assertEqual(next(asset["id"] for asset in remote.assets if asset["name"] == "package-0.apk"), original_package_id)

    def test_draft_lookup_paginates_and_uploads_to_selected_release_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            tag, _ = publisher.release_location(self.metadata(), "owner/repo")
            remote.release = dict(id=5, tag_name=tag, draft=True)
            remote.other_releases = [dict(id=1000 + i, tag_name=f"unrelated-{i}", draft=True) for i in range(100)]
            remote.other_releases += [dict(id=20, tag_name=tag, draft=True)]
            with patch.object(publisher.subprocess, "run", side_effect=remote.api_upload) as run:
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api)
            self.assertTrue(run.called)
            self.assertTrue(any(method == "GET" and "/releases?per_page=100&page=2" in endpoint for method, endpoint, _ in remote.calls))
            self.assertFalse(any(method == "POST" for method, _, _ in remote.calls))
            self.assertFalse(remote.release["draft"])

    def test_repeated_publication_preserves_packages_and_replaces_only_changed_metadata(self):
        for manager in ("apk", "opkg"):
            with self.subTest(manager=manager), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.feed(root, manager=manager)
                metadata = self.metadata(package_manager=manager)
                remote = FakeGitHub(metadata)
                publisher.publish(metadata, root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
                original = {asset["name"]: asset["id"] for asset in remote.assets}
                remote.other_releases = [dict(id=1, tag_name=remote.release["tag_name"], draft=True)]
                start = len(remote.calls)
                with patch.object(publisher.subprocess, "run", side_effect=remote.api_upload):
                    publisher.publish(metadata, root, "owner/repo", "a" * 40, "1", "2", remote.api)
                for asset in remote.assets:
                    if asset["name"] not in {"build.json", "sha256sums"}:
                        self.assertEqual(asset["id"], original[asset["name"]])
                uploaded = {name for method, names, _ in remote.calls[start:] if method == "UPLOAD" for name in names}
                self.assertEqual(uploaded, {"build.json", "sha256sums"})
                self.assertFalse(any(method == "POST" for method, _, _ in remote.calls[start:]))

    def test_retry_replaces_an_incomplete_starter_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            tag, _ = publisher.release_location(self.metadata(), "owner/repo")
            remote.release = dict(id=5, tag_name=tag, draft=True)
            remote.assets = [dict(id=99, name="package-0.apk", state="starter", size=0, digest=None)]
            with patch.object(publisher.subprocess, "run", side_effect=remote.api_upload):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "2", remote.api)
            self.assertTrue(any(method == "DELETE" and endpoint.endswith("/assets/99") for method, endpoint, _ in remote.calls))
            self.assertTrue(all(asset["state"] == "uploaded" for asset in remote.assets))
            self.assertFalse(remote.release["draft"])

    def test_existing_package_with_different_bytes_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            (root / "package-0.apk").write_bytes(b"different bytes under the same filename")
            start = len(remote.calls)
            with self.assertRaisesRegex(ValueError, "same content-addressed name has different bytes"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "2", remote.api, remote.upload)
            self.assertFalse(any(method in {"POST", "PATCH", "DELETE", "UPLOAD"} for method, _, _ in remote.calls[start:]))

    def test_release_lookup_error_does_not_create_a_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            def forbidden(endpoint, method="GET", payload=None):
                self.assertEqual(method, "GET")
                raise publisher.GitHubAPIError(403)
            with self.assertRaises(publisher.GitHubAPIError):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", forbidden)

    def test_wrong_final_url_is_rejected_after_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            def wrong_url(items):
                items[0]["browser_download_url"] = "https://github.com/other/repo/releases/download/tag/package.apk"
            remote = FakeGitHub(self.metadata(), wrong_url)
            with self.assertRaisesRegex(ValueError, "Unexpected release download URL"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            self.assertTrue(any(method == "PATCH" for method, _, _ in remote.calls))
            self.assertFalse(any(method == "DELETE" for method, _, _ in remote.calls))

    def test_post_publication_url_failure_preserves_previous_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            (root / "package-0.apk").unlink()
            (root / "package-new.apk").write_bytes(b"new package")
            (root / "packages.adb").write_bytes(b"new index")
            start = len(remote.calls)
            def corrupt_after_publish(endpoint, method="GET", payload=None):
                result = remote.api(endpoint, method, payload)
                if method == "PATCH":
                    asset = next(item for item in remote.assets if item["name"] == "package-new.apk")
                    asset["browser_download_url"] = remote.draft_url + "/package-new.apk"
                return result
            with self.assertRaisesRegex(ValueError, "Unexpected release download URL"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "2", "1", corrupt_after_publish, remote.upload)
            self.assertIn("package-0.apk", {asset["name"] for asset in remote.assets})
            self.assertFalse(any(method == "DELETE" for method, _, _ in remote.calls[start:]))

    def test_renamed_or_corrupt_assets_leave_release_unpublished(self):
        for mutate in [lambda items: items[0].update(name="renamed.apk"), lambda items: items[0].update(digest="sha256:wrong")]:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.feed(root)
                calls, publish = self.exercise_publish(root, mutate)
                with self.assertRaises(ValueError):
                    publish()
                self.assertFalse(any(method == "PATCH" for method, _, _ in calls))

    def test_rebuild_updates_same_release_and_keeps_old_packages_until_index_is_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            first_url = publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            (root / "package-0.apk").unlink()
            (root / "package-new.apk").write_bytes(b"updated package")
            (root / "packages.adb").write_bytes(b"updated index")
            start = len(remote.calls)
            next_url = publisher.publish(self.metadata(), root, "owner/repo", "b" * 40, "2", "3", remote.api, remote.upload)
            self.assertEqual(first_url, next_url)
            self.assertEqual(sum(method == "POST" for method, _, _ in remote.calls), 1)
            self.assertNotIn("package-0.apk", {asset["name"] for asset in remote.assets})
            self.assertIn("package-new.apk", {asset["name"] for asset in remote.assets})
            updates = remote.calls[start:]
            new_package = next(i for i, (method, names, _) in enumerate(updates) if method == "UPLOAD" and "package-new.apk" in names)
            new_index = next(i for i, (method, names, _) in enumerate(updates) if method == "UPLOAD" and "packages.adb" in names)
            old_deleted = next(i for i, (method, _, _) in enumerate(updates) if method == "DELETE")
            self.assertLess(new_package, new_index)
            self.assertLess(new_index, old_deleted)
            self.assertEqual(json.loads((root / "build.json").read_text())["build_run_id"], "2")

    def test_rebuild_refuses_a_new_public_key_before_changing_the_live_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            (root / "feed-key.pem").write_bytes(b"different public key")
            start = len(remote.calls)
            with self.assertRaisesRegex(ValueError, "feed key changed"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "2", "1", remote.api, remote.upload)
            self.assertFalse(any(method in {"POST", "PATCH", "DELETE", "UPLOAD"} for method, _, _ in remote.calls[start:]))

    def test_failed_live_index_upload_does_not_remove_previous_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            (root / "package-0.apk").unlink()
            (root / "package-new.apk").write_bytes(b"new package")
            (root / "packages.adb").write_bytes(b"new index")
            start = len(remote.calls)

            def fail_index(files, clobber=False):
                if any(path.name == "packages.adb" for path in files):
                    raise RuntimeError("Upload failed")
                remote.upload(files, clobber)

            with self.assertRaisesRegex(RuntimeError, "Upload failed"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "2", "1", remote.api, fail_index)
            self.assertIn("package-0.apk", {asset["name"] for asset in remote.assets})
            self.assertFalse(any(method in {"PATCH", "DELETE"} for method, _, _ in remote.calls[start:]))

    def test_immutable_release_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.feed(root)
            remote = FakeGitHub(self.metadata())
            publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "1", "1", remote.api, remote.upload)
            remote.release["immutable"] = True
            start = len(remote.calls)
            with self.assertRaisesRegex(ValueError, "editable"):
                publisher.publish(self.metadata(), root, "owner/repo", "a" * 40, "2", "1", remote.api, remote.upload)
            self.assertFalse(any(method in {"POST", "PATCH", "DELETE", "UPLOAD"} for method, _, _ in remote.calls[start:]))


@unittest.skipUnless(os.getenv("TEST_APK_SDK"), "Set TEST_APK_SDK to test native APK filename normalization")
class NativeAssetTests(unittest.TestCase):
    def test_hash_filenames_preserve_index_without_specification_warnings(self):
        native = Path(os.environ["TEST_APK_SDK"]).resolve()
        apk = str(native / "staging_dir/host/bin/apk")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk, repo, payload = [root / name for name in ("sdk", "repo", "payload")]
            for path in (sdk, repo, payload):
                path.mkdir()
            (sdk / "staging_dir").symlink_to(native / "staging_dir", target_is_directory=True)
            (sdk / "scripts").symlink_to(native / "scripts", target_is_directory=True)
            subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(sdk / "private-key.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(sdk / "private-key.pem"), "-pubout", "-out", str(sdk / "public-key.pem")], check=True, capture_output=True)
            (payload / "sample.txt").write_text("original payload")
            original = repo / "example-1.0~abc-r1.apk"
            subprocess.run([apk, "mkpkg", "--info", "name:example", "--info", "version:1.0~abc-r1", "--info", "arch:noarch", "--info", "license:MIT", "--files", str(payload), "--output", str(original)], check=True, capture_output=True)
            original_digest = publisher.sha256(original)
            subprocess.run([apk, "mkndx", "--allow-untrusted", "--sign", str(sdk / "private-key.pem"), "--output", str(repo / "packages.adb"), str(original)], check=True, capture_output=True)
            before = json.loads(subprocess.check_output([apk, "adbdump", "--format", "json", str(repo / "packages.adb")]))
            (repo / "build.json").write_text(json.dumps({"arch": "aarch64_generic"}))
            result = subprocess.run(["python3", str(ROOT / "scripts/feed-assets.py"), "apk", str(repo), str(sdk)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("not matching package name specification", result.stderr)
            renamed = list(repo.glob("*.apk"))
            self.assertEqual(len(renamed), 1)
            self.assertEqual(publisher.sha256(renamed[0]), original_digest)
            after = json.loads(subprocess.check_output([apk, "adbdump", "--format", "json", str(repo / "packages.adb")]))
            self.assertEqual(after["packages"], before["packages"])
            self.assertEqual(renamed[0].name, "example-" + after["packages"][0]["hashes"] + ".apk")
            subprocess.run([apk, "--keys-dir", str(sdk), "verify", str(repo / "packages.adb")], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()

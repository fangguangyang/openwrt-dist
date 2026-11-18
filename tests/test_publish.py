import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

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

    def api(self, endpoint, method="GET", payload=None):
        self.calls.append((method, endpoint, payload))
        if "/releases/tags/" in endpoint:
            if self.release is None:
                raise publisher.GitHubAPIError(404)
            return dict(self.release)
        if method == "POST":
            self.release = dict(payload, id=5)
            return dict(self.release)
        if method == "DELETE":
            asset_id = int(endpoint.rsplit("/", 1)[1])
            self.assets = [asset for asset in self.assets if asset["id"] != asset_id]
            return None
        if method == "GET":
            page = int(endpoint.rsplit("=", 1)[1])
            return self.assets[(page - 1) * 100:page * 100]
        self.release.update(payload)
        return dict(self.release)

    def upload(self, files, clobber=False):
        self.calls.append(("UPLOAD", [path.name for path in files], clobber))
        for path in files:
            existing = next((asset for asset in self.assets if asset["name"] == path.name), None)
            if existing:
                if not clobber:
                    raise AssertionError("Attempted to overwrite an existing package")
                self.assets.remove(existing)
            self.assets.append(dict(id=self.next_id, name=path.name, state="uploaded", size=path.stat().st_size,
                                    browser_download_url=self.url + "/" + path.name, digest="sha256:" + publisher.sha256(path)))
            self.next_id += 1
        if self.corrupt and any(path.name == "packages.adb" for path in files):
            self.corrupt(self.assets)


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
            self.assertEqual(calls[-1][0], "PATCH")
            self.assertFalse(calls[-1][2]["draft"])

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


if __name__ == "__main__":
    unittest.main()

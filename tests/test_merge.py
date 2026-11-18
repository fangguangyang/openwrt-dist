import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("merge_feed", ROOT / "scripts/merge-feed.py")
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


class MergeTests(unittest.TestCase):
    def test_group_partition_covers_the_package_manifest_once(self):
        groups = json.loads((ROOT / "package-groups.json").read_text())
        requested = [s.strip() for s in (ROOT / "packages.txt").read_text().splitlines() if s.strip() and not s.startswith("#")]
        partition = [package for packages in groups.values() for package in packages]
        self.assertCountEqual(partition, requested)
        self.assertEqual(len(partition), len(set(partition)))

    def test_duplicate_dependencies_are_deduplicated_but_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for group in ("one", "two"):
                (root / "parts" / group).mkdir(parents=True)
                (root / "parts" / group / "lib.apk").write_bytes(b"same dependency")
            self.assertEqual(merge.merge(root / "parts", root / "output", "apk"), 1)
            (root / "parts/two/lib.apk").write_bytes(b"different dependency")
            with self.assertRaisesRegex(ValueError, "Conflicting packages"):
                merge.merge(root / "parts", root / "output", "apk")

    def test_empty_feed_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "No compiled packages"):
                merge.merge(root / "parts", root / "output", "opkg")

    def test_official_dependency_variants_are_not_republished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for group in ("mosdns", "openlist"):
                part = root / "parts" / group
                part.mkdir(parents=True)
                (part / "luci-base-1.0.apk").write_bytes(group.encode())
                (part / f"luci-app-{group}-1.0.apk").write_bytes(b"requested application")
            def metadata(path, *_):
                name = path.name.removesuffix("-1.0.apk")
                return dict(name=name, version="1.0", origin="feeds/luci/feeds/luci/modules/" + name)
            report = root / "output/merge-report.json"
            wanted = {"luci-app-mosdns", "luci-app-openlist"}
            with patch.object(merge, "package_metadata", side_effect=metadata):
                self.assertEqual(merge.merge(root / "parts", root / "output", "apk", wanted, report=report), 2)
            self.assertEqual({p.name for p in (root / "output").glob("*.apk")}, {name + "-1.0.apk" for name in wanted})
            excluded = json.loads(report.read_text())["excluded_official_dependencies"]
            self.assertEqual(len(excluded), 2)
            self.assertTrue(all(item["package"] == "luci-base" for item in excluded))

    def test_explicitly_requested_stock_package_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parts").mkdir()
            (root / "parts/luci-base-1.0.apk").write_bytes(b"explicit request")
            metadata = dict(name="luci-base", version="1.0", origin="feeds/luci/modules/luci-base")
            with patch.object(merge, "package_metadata", return_value=metadata):
                self.assertEqual(merge.merge(root / "parts", root / "output", "apk", {"luci-base"}), 1)
            self.assertTrue((root / "output/luci-base-1.0.apk").exists())

    def test_ipk_control_origin_filters_only_unrequested_official_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "parts"
            source.mkdir()
            for name, origin in (("luci-base", "feeds/luci/modules/luci-base"),
                                 ("luci-app-mosdns", "feeds/mosdns/luci-app-mosdns")):
                text = f"Package: {name}\nVersion: 1.0-r1\nSource: {origin}\nArchitecture: all\n".encode()
                control = io.BytesIO()
                with tarfile.open(fileobj=control, mode="w:gz") as archive:
                    member = tarfile.TarInfo("./control")
                    member.size = len(text)
                    archive.addfile(member, io.BytesIO(text))
                with tarfile.open(source / (name + "_1.0-r1_all.ipk"), mode="w:gz") as outer:
                    data = control.getvalue()
                    member = tarfile.TarInfo("./control.tar.gz")
                    member.size = len(data)
                    outer.addfile(member, io.BytesIO(data))
            self.assertEqual(merge.merge(source, root / "output", "opkg", {"luci-app-mosdns"}), 1)
            self.assertEqual([p.name for p in (root / "output").glob("*.ipk")], ["luci-app-mosdns_1.0-r1_all.ipk"])

    def test_custom_dependency_conflicts_still_fail_before_any_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for group in ("one", "two"):
                part = root / "parts" / group
                part.mkdir(parents=True)
                (part / "mosdns-1.0.apk").write_bytes(group.encode())
            metadata = dict(name="mosdns", version="1.0", origin="feeds/mosdns/mosdns")
            report = root / "output/merge-report.json"
            with patch.object(merge, "package_metadata", return_value=metadata):
                with self.assertRaisesRegex(ValueError, "Conflicting packages"):
                    merge.merge(root / "parts", root / "output", "apk", set(), report=report)
            self.assertFalse(list((root / "output").glob("*.apk")))
            self.assertEqual(len(json.loads(report.read_text())["conflicts"]), 1)

    def test_device_extra_packages_are_requested_and_missing_packages_fail(self):
        packages = merge.requested_packages(ROOT, "m28c")
        self.assertTrue({"luci-theme-design", "xmm-modem", "luci-proto-xmm"} <= packages)
        self.assertNotIn("luci-base", packages)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parts").mkdir()
            (root / "parts/luci-app-mosdns-1.0.apk").write_bytes(b"one app")
            metadata = dict(name="luci-app-mosdns", version="1.0", origin="feeds/mosdns/luci-app-mosdns")
            with patch.object(merge, "package_metadata", return_value=metadata):
                with self.assertRaisesRegex(ValueError, "Missing requested packages: luci-app-openlist2"):
                    merge.merge(root / "parts", root / "output", "apk", {"luci-app-mosdns", "luci-app-openlist2"})
            self.assertFalse(list((root / "output").glob("*.apk")))

    def test_missing_group_or_wrong_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = dict(distro="immortalwrt", version="25.12.2", target="x86/64", arch="x86_64", package_manager="apk", version_code="r1")
            (root / "build.json").write_text(json.dumps(dict(metadata, group="one")))
            (root / "sources.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "Software groups do not match"):
                merge.validate_parts(root, metadata, ["one", "two"])
            with self.assertRaisesRegex(ValueError, "metadata mismatch"):
                merge.validate_parts(root, dict(metadata, version="24.10.6"), ["one"])
            merge.validate_parts(root, metadata, ["one"])


@unittest.skipUnless(os.getenv("TEST_APK_SDK"), "Set TEST_APK_SDK to test native APK merging")
class NativeMergeTests(unittest.TestCase):
    def test_real_apks_merge_and_resolve_stock_dependency_from_separate_feed(self):
        apk = str(Path(os.environ["TEST_APK_SDK"]).resolve() / "staging_dir/host/bin/apk")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parts, output, official = [root / name for name in ("parts", "output", "official")]
            official.mkdir()
            wanted = {"luci-app-mosdns", "luci-app-openlist2"}
            def build(name, path, origin, content, depends=None):
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = root / f"payload-{len(list(root.glob('payload-*')))}"
                payload.mkdir()
                (payload / (name + ".txt")).write_text(content)
                command = [apk, "mkpkg", "--info", "name:" + name, "--info", "version:1.0-r1",
                           "--info", "arch:noarch", "--info", "license:MIT", "--info", "origin:" + origin,
                           "--files", str(payload), "--output", str(path)]
                if depends:
                    command += ["--info", "depends:" + depends]
                subprocess.run(command, check=True, capture_output=True)
            for index, app in enumerate(sorted(wanted)):
                group = parts / str(index)
                build(app, group / (app + "-1.0-r1.apk"), "feeds/custom/" + app, app, "luci-base")
                build("luci-base", group / "luci-base-1.0-r1.apk", "feeds/luci/feeds/luci/modules/luci-base", "different group " + str(index))
            build("luci-base", official / "luci-base-1.0-r1.apk", "feeds/luci/modules/luci-base", "official runtime")
            self.assertEqual(merge.merge(parts, output, "apk", wanted, apk, output / "merge-report.json"), 2)
            self.assertFalse((output / "luci-base-1.0-r1.apk").exists())
            for repo in (output, official):
                subprocess.run([apk, "mkndx", "--allow-untrusted", "--output", str(repo / "packages.adb"),
                                *map(str, repo.glob("*.apk"))], check=True, capture_output=True)
            downloads = root / "downloads"
            downloads.mkdir()
            package_root = root / "apk-root"
            package_root.mkdir()
            subprocess.run([apk, "--root", str(package_root), "--allow-untrusted", "--repositories-file", "/dev/null", "--arch", "aarch64_generic",
                            "--repository", str(output / "packages.adb"), "--repository", str(official / "packages.adb"),
                            "--no-cache", "fetch", "--recursive", "--output", str(downloads), *sorted(wanted)], check=True)
            self.assertEqual({p.name for p in downloads.glob("*.apk")}, {name + "-1.0-r1.apk" for name in wanted | {"luci-base"}})
            self.assertEqual((downloads / "luci-base-1.0-r1.apk").read_bytes(), (official / "luci-base-1.0-r1.apk").read_bytes())


if __name__ == "__main__":
    unittest.main()

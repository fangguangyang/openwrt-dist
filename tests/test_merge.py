import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()

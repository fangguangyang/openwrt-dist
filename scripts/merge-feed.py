#!/usr/bin/env python3
"""Merge independently compiled software groups, rejecting ambiguous outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def validate_parts(source, expected, groups):
    seen = set()
    sources = set()
    for path in source.rglob("build.json"):
        actual = json.loads(path.read_text())
        for key in ("distro", "version", "target", "arch", "package_manager", "version_code"):
            if actual[key] != expected[key]:
                raise ValueError(f"Software group metadata mismatch: {path}: {key}")
        if actual["group"] in seen:
            raise ValueError("Duplicate software group: " + actual["group"])
        seen.add(actual["group"])
        sources.add((path.parent / "sources.json").read_bytes())
    if seen != set(groups):
        raise ValueError(f"Software groups do not match: expected {set(groups)}, found {seen}")
    if len(sources) != 1:
        raise ValueError("Software groups used different source feed commits")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def merge(source, destination, manager):
    destination.mkdir(parents=True, exist_ok=True)
    extension = "apk" if manager == "apk" else "ipk"
    count = 0
    for package in sorted(source.rglob("*." + extension)):
        output = destination / package.name
        if output.exists():
            if digest(output) != digest(package):
                raise ValueError("Conflicting packages from software groups: " + package.name)
        else:
            shutil.copy2(package, output)
            count += 1
    if not count:
        raise ValueError("No compiled packages found")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    groups = json.loads((Path(__file__).resolve().parent.parent / "package-groups.json").read_text())
    validate_parts(args.source, metadata, groups)
    print(merge(args.source, args.destination, metadata["package_manager"]))

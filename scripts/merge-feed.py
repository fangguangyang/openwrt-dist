#!/usr/bin/env python3
"""Publish requested/custom packages; resolve stock dependencies from official feeds."""
import argparse
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

UPSTREAM_FEEDS = {"base", "base_root", "luci", "packages", "routing", "telephony"}


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


def package_metadata(package, manager, apk=None):
    if manager == "apk":
        if not apk:
            raise ValueError("The SDK apk tool is required to inspect package origins")
        data = json.loads(subprocess.check_output([str(apk), "adbdump", "--format", "json", str(package)], text=True))
        return data["info"]
    # OpenWrt ipks contain a control archive inside a tar or ar container.
    try:
        with tarfile.open(package, "r:*") as outer:
            member = next(item for item in outer.getmembers() if item.name.lstrip("./") == "control.tar.gz")
            control = outer.extractfile(member).read()
    except tarfile.ReadError:
        control = subprocess.check_output(["ar", "p", str(package), "control.tar.gz"])
    with tarfile.open(fileobj=io.BytesIO(control), mode="r:*") as archive:
        member = next(item for item in archive.getmembers() if item.name.lstrip("./") == "control")
        fields = Parser().parsestr(archive.extractfile(member).read().decode())
    return {"name": fields["Package"], "version": fields["Version"], "origin": fields.get("Source", "")}


def official_dependency(metadata, requested):
    if metadata["name"] in requested:
        return False
    origin = metadata.get("origin", "")
    match = re.search(r"(?:^|/)feeds/([^/]+)/", origin)
    return bool(match and match[1] in UPSTREAM_FEEDS)


def requested_packages(root, device):
    groups = json.loads((root / "package-groups.json").read_text())
    packages = {name for group in groups.values() for name in group}
    if device == "m28c":
        packages.update(line.split("#", 1)[0].strip() for line in (root / "packages-m28c.txt").read_text().splitlines())
    packages.discard("")
    return packages


def merge(source, destination, manager, requested=None, apk=None, report=None):
    destination.mkdir(parents=True, exist_ok=True)
    extension = "apk" if manager == "apk" else "ipk"
    selected = {}
    found = set()
    audit = {"excluded_official_dependencies": [], "selected": [], "duplicates": [], "conflicts": []}

    def save_report():
        if report:
            report.write_text(json.dumps(audit, indent=2) + "\n")

    for package in sorted(source.rglob("*." + extension)):
        if requested is not None:
            metadata = package_metadata(package, manager, apk)
            if official_dependency(metadata, requested):
                audit["excluded_official_dependencies"].append({
                    "file": str(package.relative_to(source)), "package": metadata["name"],
                    "version": metadata["version"], "origin": metadata.get("origin", ""),
                })
                continue
            found.add(metadata["name"])
        previous = selected.get(package.name)
        if previous:
            files = [str(previous.relative_to(source)), str(package.relative_to(source))]
            if digest(previous) != digest(package):
                audit["conflicts"].append({"filename": package.name, "files": files})
                save_report()
                raise ValueError("Conflicting packages from software groups: " + package.name +
                                 " (" + ", ".join(files) + ")")
            audit["duplicates"].append({"filename": package.name, "files": files})
        else:
            selected[package.name] = package
            audit["selected"].append(str(package.relative_to(source)))
    if not selected:
        save_report()
        raise ValueError("No compiled packages found")
    if requested is not None:
        missing = requested - found
        if missing:
            save_report()
            raise ValueError("Missing requested packages: " + ", ".join(sorted(missing)))
    # Validate every group before copying anything into the published feed.
    for name, package in selected.items():
        output = destination / name
        if output.exists() and digest(output) != digest(package):
            raise ValueError("Conflicting package already in merge destination: " + name)
    for name, package in selected.items():
        shutil.copy2(package, destination / name)
    save_report()
    return len(selected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--apk", type=Path, help="SDK apk tool for reading native package metadata")
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    groups = json.loads((Path(__file__).resolve().parent.parent / "package-groups.json").read_text())
    validate_parts(args.source, metadata, groups)
    requested = requested_packages(Path(__file__).resolve().parent.parent, metadata["id"])
    report = args.destination / "merge-report.json"
    count = merge(args.source, args.destination, metadata["package_manager"], requested, args.apk, report)
    excluded = len(json.loads(report.read_text())["excluded_official_dependencies"])
    print(f"Merged {count} packages; left {excluded} stock dependency copies to the matching official feeds")

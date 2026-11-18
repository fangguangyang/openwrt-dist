#!/usr/bin/env python3
"""Fetch OpenClash/PassWall from the matching official ImageBuilder feeds."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import release

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"openclash", "passwall"}


def repositories(builder, manager):
    path = builder / ("repositories" if manager == "apk" else "repositories.conf")
    # Keep the official ImageBuilder's release/architecture URLs and keys.
    # Only luci and packages contain this group's explicitly requested packages.
    lines = []
    for line in path.read_text().splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        url = value if manager == "apk" else value.split()[-1]
        parts = url.rstrip("/").split("/")
        feed = parts[-2] if manager == "apk" else parts[-1]
        if url.startswith("https://") and "/targets/" not in url and feed in {"luci", "packages"}:
            lines.append(value)
    if not lines:
        raise ValueError("ImageBuilder has no official package repositories")
    if manager == "opkg":
        lines.append("option check_signature 1")
    return "\n".join(lines) + "\n"


def fetch_packages(metadata, builder, destination, packages, state):
    manager = metadata["package_manager"]
    config = state / ("repositories" if manager == "apk" else "repositories.conf")
    config.write_text(repositories(builder, manager))
    package_root = state / "root"
    package_root.mkdir()
    cache = state / "cache"
    cache.mkdir()
    if manager == "apk":
        command = [str(builder / "staging_dir/host/bin/apk"), "--root", str(package_root),
                   "--arch", metadata["arch"], "--keys-dir", str(builder / "keys"),
                   "--repositories-file", str(config), "--cache-dir", str(cache),
                   "fetch", "--output", str(destination), *packages]
    elif manager == "opkg":
        # ImageBuilder's opkg wrapper verifies repository signatures with these keys.
        command = [str(builder / "staging_dir/host/bin/opkg"), "--offline-root", str(package_root),
                   "--conf", str(config), "--lists-dir", str(cache),
                   "--add-arch", "all:100", "--add-arch", metadata["arch"] + ":200",
                   "--verify-program", str(builder / "scripts/opkg-key")]
    else:
        raise ValueError("Unsupported package manager: " + manager)
    environment = dict(os.environ, OPKG_KEYS=str(builder / "keys"),
                       PATH=str(builder / "staging_dir/host/bin") + os.pathsep + os.environ.get("PATH", ""))
    if manager == "opkg":
        subprocess.run([*command, "update"], cwd=destination, env=environment, check=True)
        command += ["download", *packages]
    # Deliberately do not fetch recursively: ordinary dependencies remain in the
    # device's official feeds, without duplicating libraries built by other groups.
    subprocess.run(command, cwd=destination, env=environment, check=True)
    return config.read_text()


def inventory(destination, packages, manager, arch):
    extension = "apk" if manager == "apk" else "ipk"
    files = sorted(destination.glob("*." + extension))
    records = []
    matched = set()
    for path in files:
        if manager == "apk":
            owners = [name for name in packages if re.fullmatch(re.escape(name) + r"-[0-9].*\.apk", path.name)]
        else:
            owners = [name for name in packages if re.fullmatch(re.escape(name) + r"_[^/]+_(?:" + re.escape(arch) + r"|all|noarch)\.ipk", path.name)]
        if len(owners) != 1 or owners[0] in matched:
            raise ValueError("Unexpected or duplicate prebuilt package: " + path.name)
        matched.add(owners[0])
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append({"package": owners[0], "filename": path.name, "sha256": digest})
    if matched != set(packages):
        raise ValueError("Official feeds have no prebuilt packages for this release/architecture: " +
                         ", ".join(sorted(set(packages) - matched)))
    return records


def fetch_group(metadata, group, destination, sources):
    if group not in GROUPS:
        raise ValueError("Only openclash and passwall use prebuilt packages")
    source_lock = sources.read_bytes()
    packages = json.loads((ROOT / "package-groups.json").read_text())[group]
    work = ROOT / ".build" / metadata["id"]
    destination.mkdir(parents=True, exist_ok=True)
    if list(destination.glob("*.apk")) or list(destination.glob("*.ipk")):
        raise ValueError("Prebuilt output directory must not contain old packages")
    archive = release.download(metadata, "imagebuilder", work / "downloads")
    with tempfile.TemporaryDirectory(prefix="prebuilt-", dir=work) as temporary:
        state = Path(temporary)
        builder = state / "builder"
        builder.mkdir()
        subprocess.run(["tar", "-xf", str(archive), "--strip-components=1", "-C", str(builder)], check=True)
        print(f"Fetching official {metadata['distro']} {metadata['version']} / {metadata['arch']} / {group}", flush=True)
        feeds = fetch_packages(metadata, builder, destination, packages, state)
        records = inventory(destination, packages, metadata["package_manager"], metadata["arch"])
    provenance = {"mode": "official-prebuilt", "imagebuilder": metadata["imagebuilder"],
                  "repositories": feeds.splitlines(), "packages": records,
                  "dependencies": "Resolved from the matching official system feeds"}
    (destination / "prebuilt.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (destination / "build.json").write_text(json.dumps(dict(metadata, group=group, mode="official-prebuilt"), indent=2) + "\n")
    (destination / "sdk.config").write_text("# Official prebuilt packages; no SDK compilation.\n# Versions and SHA256: prebuilt.json\n")
    (destination / "sources.json").write_bytes(source_lock)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("group", choices=sorted(GROUPS))
    parser.add_argument("destination", type=Path)
    parser.add_argument("sources", type=Path)
    args = parser.parse_args()
    fetch_group(json.loads(args.metadata.read_text()), args.group, args.destination.resolve(), args.sources.resolve())

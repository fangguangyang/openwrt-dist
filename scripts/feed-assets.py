#!/usr/bin/env python3
"""Use release-safe asset names and keep package indexes consistent with them."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalize_ipk_names(directory):
    for package in sorted(directory.glob("*.ipk")):
        checksum = digest(package)[:12]
        if package.stem.endswith("-" + checksum) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package.name):
            continue
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", package.stem).strip(".")
        output = directory / f"{stem}-{checksum}.ipk"
        if output.exists():
            raise ValueError("Duplicate release asset: " + output.name)
        package.rename(output)


def normalize_apk_names(directory, sdk):
    apk = str(sdk / "staging_dir/host/bin/apk")
    index = json.loads(subprocess.check_output([apk, "adbdump", "--format", "json", str(directory / "packages.adb")], text=True))
    for package in index["packages"]:
        name, version, checksum = [package[k] for k in ("name", "version", "hashes")]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or not re.fullmatch(r"[a-f0-9]{40,64}", checksum):
            raise ValueError("Unsupported release package name or hash: " + name)
        original = directory / f"{name}-{version}.apk"
        output = directory / f"{name}-{checksum}.apk"
        if output.exists():
            raise ValueError("Duplicate release asset: " + output.name)
        original.rename(output)
    # Index schema fields use "hashes"; the download URL formatter uses "hash".
    # Reuse verified entries using their schema field, then write the URL spec.
    subprocess.run([apk, "--keys-dir", str(sdk), "verify", str(directory / "packages.adb")], check=True)
    temporary = directory / "packages.normalized.adb"
    subprocess.run([apk, "mkndx", "--keys-dir", str(sdk), "--sign", str(sdk / "private-key.pem"),
                    "--index", "packages.adb", "--filter-spec", "${name}-${hashes}.apk",
                    "--pkgname-spec", "${name}-${hash}.apk", "--output", temporary.name,
                    *[p.name for p in sorted(directory.glob("*.apk"))]], cwd=directory, check=True)
    normalized = json.loads(subprocess.check_output([apk, "adbdump", "--format", "json", str(temporary)], text=True))
    if normalized.get("packages", []) != index["packages"]:
        raise ValueError("APK filename normalization changed the indexed packages")
    temporary.replace(directory / "packages.adb")
    data = subprocess.check_output([apk, "adbdump", "--format", "json", str(directory / "packages.adb")])
    metadata = json.loads((directory / "build.json").read_text())
    with (directory / "index.json").open("wb") as output:
        subprocess.run(["python3", str(sdk / "scripts/make-index-json.py"), "-f", "apk", "-a", metadata["arch"], "-"],
                       input=data, stdout=output, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manager", choices=["apk", "opkg"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("sdk", type=Path)
    args = parser.parse_args()
    if args.manager == "apk":
        normalize_apk_names(args.directory.resolve(), args.sdk.resolve())
    else:
        normalize_ipk_names(args.directory.resolve())

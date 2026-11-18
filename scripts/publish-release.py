#!/usr/bin/env python3
"""Publish a signed feed as individual assets in a complete GitHub Release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_location(metadata, repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    components = [metadata["distro"], metadata["version"], metadata["target"].replace("/", "-")]
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in components):
        raise ValueError("Invalid release tag component")
    tag = "packages-" + "-".join(components)
    return tag, f"https://github.com/{repository}/releases/download/{tag}"


def prepare_assets(directory, metadata, tag, feed_url):
    manager = metadata["package_manager"]
    required = ["packages.adb", "feed-key.pem"] if manager == "apk" else ["Packages", "Packages.gz", "Packages.sig", "feed-key.pub"]
    for name in [*required, "sources.json"]:
        if not (directory / name).is_file() or not (directory / name).stat().st_size:
            raise ValueError("Missing feed asset: " + name)
    extension = "apk" if manager == "apk" else "ipk"
    if not list(directory.glob("*." + extension)):
        raise ValueError("No compiled packages to publish")
    (directory / "build.json").write_text(json.dumps(dict(metadata, release_tag=tag, feed_url=feed_url), indent=2) + "\n")
    configs = directory / "build-configs"
    if configs.is_dir():
        with tarfile.open(directory / "build-configs.tar.gz", "w:gz") as archive:
            archive.add(configs, arcname="build-configs")
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "sha256sums")
    for path in files:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name):
            raise ValueError("Unsafe GitHub asset filename: " + path.name)
        if path.name.endswith((".pem", ".key")) and b"PRIVATE KEY" in path.read_bytes():
            raise ValueError("Refusing to publish a private key: " + path.name)
        if path.stat().st_size >= 2 * 1024 ** 3:
            raise ValueError("GitHub asset must be below 2 GiB: " + path.name)
    checksums = directory / "sha256sums"
    checksums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in files))
    files.append(checksums)
    if len(files) > 1000:
        raise ValueError("GitHub permits at most 1000 assets per release; reduce the package selection")
    return files


class GitHubAPIError(RuntimeError):
    def __init__(self, status, message="GitHub API request failed"):
        super().__init__(f"{message} (HTTP {status})")
        self.status = status


def gh_api(endpoint, method="GET", payload=None):
    command = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=None if payload is None else json.dumps(payload), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        value = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        value = None
    if result.returncode:
        match = re.search(r"HTTP (\d+)", result.stderr)
        status = int(value.get("status") or 0) if isinstance(value, dict) else 0
        if not status and match:
            status = int(match[1])
        raise GitHubAPIError(status, value.get("message", "GitHub API request failed") if isinstance(value, dict) else "GitHub API request failed")
    return value


def validate_uploaded(files, uploaded, feed_url, allow_extra=False, check_urls=True):
    actual = {asset["name"]: asset for asset in uploaded}
    wanted = {path.name for path in files}
    if len(actual) != len(uploaded) or not wanted <= set(actual) or (not allow_extra and set(actual) != wanted):
        raise ValueError("Uploaded release filenames differ from the signed repository")
    for path in files:
        asset = actual[path.name]
        if asset["state"] != "uploaded" or asset["size"] != path.stat().st_size:
            raise ValueError("Incomplete release upload: " + path.name)
        expected_url = feed_url + "/" + path.name
        if check_urls and asset.get("browser_download_url") != expected_url:
            raise ValueError(f"Unexpected release download URL: {path.name}: "
                             f"expected {expected_url!r}, got {asset.get('browser_download_url')!r}")
        if asset.get("digest") != "sha256:" + sha256(path):
            raise ValueError("Release asset checksum mismatch: " + path.name)


def list_assets(repository, release_id, api):
    uploaded = []
    page = 1
    while True:
        batch = api(f"repos/{repository}/releases/{release_id}/assets?per_page=100&page={page}")
        uploaded.extend(batch)
        if len(batch) < 100:
            return uploaded
        page += 1


def same_asset(asset, path):
    return (asset.get("state") == "uploaded" and asset.get("size") == path.stat().st_size
            and asset.get("digest") == "sha256:" + sha256(path))


def publish(metadata, directory, repository, source_commit, run_id, attempt, api=gh_api, upload=None):
    tag, feed_url = release_location(metadata, repository)
    metadata = dict(metadata, source_commit=source_commit, build_run_id=str(run_id), build_attempt=str(attempt))
    files = prepare_assets(directory, metadata, tag, feed_url)
    body = (f"Signed {metadata['package_manager']} feed for {metadata['distro']} {metadata['version']} / {metadata['target']}.\n\n"
            f"Feed URL: {feed_url}\n\n"
            "Use packages.adb for apk or src/gz with this directory for opkg. Import the matching feed-key first. "
            "Each package is an individual asset. Rebuilds update this feed with the same public key.\n")
    name = f"Packages {metadata['distro']} {metadata['version']} {metadata['target']}"
    try:
        current = api(f"repos/{repository}/releases/tags/{tag}")
    except GitHubAPIError as error:
        if error.status != 404:
            raise
        current = api(f"repos/{repository}/releases", "POST", {
            "tag_name": tag, "target_commitish": source_commit, "name": name,
            "body": body, "draft": True, "prerelease": metadata["version"] == "SNAPSHOT", "make_latest": "false",
        })
    if current.get("immutable"):
        raise ValueError("The fixed feed Release must be editable; this repository has immutable releases enabled")
    original = list_assets(repository, current["id"], api)
    existing = {asset["name"]: asset for asset in original}
    key_name = "feed-key.pem" if metadata["package_manager"] == "apk" else "feed-key.pub"
    if not current.get("draft", False) and not same_asset(existing.get(key_name, {}), directory / key_name):
        raise ValueError("The published feed key changed; reuse the original signing Secret to keep existing devices working")
    if len(existing) + sum(path.name not in existing for path in files) > 1000:
        raise ValueError("Not enough Release asset slots to stage the new feed without removing current packages")
    packages = [path for path in files if path.suffix in {".apk", ".ipk"}]
    for path in packages:
        if path.name in existing and not same_asset(existing[path.name], path):
            raise ValueError("A package asset with the same content-addressed name has different bytes: " + path.name)
    if upload is None:
        def upload(paths, clobber=False):
            subprocess.run(["gh", "release", "upload", tag, "--repo", repository,
                            *(["--clobber"] if clobber else []), *[str(p) for p in paths]], check=True)
    indexes = {"packages.adb", "Packages", "Packages.gz", "Packages.sig"}
    phases = [packages, [p for p in files if p not in packages and p.name not in indexes],
              [p for p in files if p.name in indexes]]
    for position, phase in enumerate(phases):
        changed = [p for p in phase if not same_asset(existing.get(p.name, {}), p)]
        if changed:
            upload(changed, clobber=position > 0)
    # Draft assets can have temporary /untagged-.../ URLs. Verify all content
    # before publishing, then require the final URLs after the draft is public.
    validate_uploaded(files, list_assets(repository, current["id"], api), feed_url,
                      allow_extra=True, check_urls=not current.get("draft", False))
    api(f"repos/{repository}/releases/{current['id']}", "PATCH", {
        "draft": False, "make_latest": "false", "name": name, "body": body,
    })
    validate_uploaded(files, list_assets(repository, current["id"], api), feed_url, allow_extra=True)
    # Old package assets remain available until the public URLs are verified.
    wanted = {path.name for path in files}
    for asset in original:
        if asset["name"].endswith((".apk", ".ipk")) and asset["name"] not in wanted:
            api(f"repos/{repository}/releases/assets/{asset['id']}", "DELETE")
    return feed_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("repository")
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    root = Path(__file__).resolve().parent.parent
    source_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    feed_url = publish(metadata, args.directory.resolve(), args.repository, source_commit,
                       os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    output = Path(os.environ["GITHUB_WORKSPACE"]) / "feed-results" / (metadata["id"] + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({metadata["id"]: feed_url}) + "\n")
    print(feed_url)


if __name__ == "__main__":
    main()

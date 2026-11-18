#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
metadata=$(realpath "${1:?Usage: sign-feed.sh metadata.json parts-directory}")
parts=$(realpath "${2:?Package parts directory required}")
device=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$metadata")
manager=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_manager"])' "$metadata")
repo="$root/repository/$device"
python3 "$root/scripts/merge-feed.py" "$metadata" "$parts" "$repo"
cp "$metadata" "$repo/build.json"
cd "$root/.build/$device/sdk"
make defconfig
python3 "$root/scripts/signing-key.py" install "$manager" "$PWD"
if [[ $manager == opkg ]]; then
    python3 "$root/scripts/feed-assets.py" opkg "$repo" "$PWD"
fi
make package/index "PACKAGE_SUBDIRS=$repo" CONFIG_SIGNED_PACKAGES=y
if [[ $manager == apk ]]; then
    python3 "$root/scripts/feed-assets.py" apk "$repo" "$PWD"
    test -s "$repo/packages.adb"
    cp public-key.pem "$repo/feed-key.pem"
    staging_dir/host/bin/apk --keys-dir "$PWD" verify "$repo/packages.adb"
else
    test -s "$repo/Packages.gz"
    test -s "$repo/Packages.sig"
    cp key-build.pub "$repo/feed-key.pub"
    staging_dir/host/bin/usign -V -p key-build.pub -m "$repo/Packages" -x "$repo/Packages.sig"
fi
cp "$metadata" "$repo/build.json"
cp sources.json "$repo/sources.json"
mkdir -p "$repo/build-configs"
for part in "$parts"/*; do
    cp "$part/sdk.config" "$repo/build-configs/$(basename "$part").config"
done

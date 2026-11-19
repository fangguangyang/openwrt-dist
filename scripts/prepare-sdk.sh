#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
metadata=$(realpath "${1:?Usage: prepare-sdk.sh metadata.json}")
device=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$metadata")
work="$root/.build/$device"
mkdir -p "$work/sdk" "$work/downloads"
archive=$(python3 "$root/scripts/release.py" download --metadata "$metadata" --kind sdk --directory "$work/downloads")
tar -xf "$archive" --strip-components=1 -C "$work/sdk"
cd "$work/sdk"
python3 - "$metadata" <<'PY'
from pathlib import Path
import json, sys
epoch = json.load(open(sys.argv[1]))['source_date_epoch']
if not isinstance(epoch, int) or epoch <= 0:
    raise SystemExit('Official profile has no valid source_date_epoch')
Path('version.date').write_text(str(epoch) + '\n')
PY
cat "$root/feeds.conf.custom" >> feeds.conf.default
if [[ $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["distro"])' "$metadata") == openwrt ]]; then
    branch=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_branch"])' "$metadata")
    [[ $branch != main ]] || branch=master
    # These LuCI applications live in ImmortalWrt rather than upstream LuCI.
    printf 'src-git immortal_luci https://github.com/immortalwrt/luci.git;%s\n' "$branch" >> feeds.conf.default
fi
./scripts/feeds update -a
# Rust CI LLVM artifacts can expire. Build the bundled LLVM source instead,
# matching upstream OpenWrt rather than depending on that temporary download.
python3 - <<'PY'
from pathlib import Path
import re
path = Path('feeds/packages/lang/rust/Makefile')
text, count = re.subn(r'--set=llvm\.download-ci-llvm=\S+',
                      '--set=llvm.download-ci-llvm=false', path.read_text())
if count != 1:
    raise SystemExit('Expected one llvm.download-ci-llvm option in the Rust recipe')
path.write_text(text)
PY
# A repository with its Makefile at the root is a single package, not a feed:
# the SDK feed scanner expects recipes in subdirectories.
python3 - "$root/single-packages.json" <<'PY'
from pathlib import Path
import json, subprocess, sys
for name, source in json.load(open(sys.argv[1])).items():
    destination = Path('package/custom') / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'clone', '--depth', '1', '--branch', source['branch'], source['repository'], str(destination)], check=True)
PY
# Design's upstream date suffix uses '-' which is not a valid apk version.
# Keep the source archive names unchanged; only this theme has no source tarball.
manager=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_manager"])' "$metadata")
if [[ $manager == apk ]]; then
    python3 - <<'PY'
from pathlib import Path
import re
path = Path('package/custom/luci-theme-design/Makefile')
path.write_text(re.sub(r'(?m)^(PKG_VERSION:=\d+\.\d+\.\d+)-(\d{8})$', r'\1.\2', path.read_text()))
PY
fi
# SDK libc choices are frozen in Config-build.in. With conditional LDAP
# dependencies, curl's outer menu guard creates a Kconfig dependency cycle.
# Expose its options in the SDK menu without changing package dependencies.
python3 - <<'PY'
from pathlib import Path
path = Path('feeds/packages/net/curl/Config.in')
text = path.read_text()
if text.startswith('if PACKAGE_libcurl\n') and text.rstrip().endswith('endif'):
    lines = text.rstrip().splitlines()
    path.write_text('\n'.join(lines[1:-1]) + '\n')
PY
# Share one feed snapshot across every software group for this architecture.
git clone --depth 1 --filter=blob:none --sparse https://github.com/openwrt/packages.git "$work/golang-source"
git -C "$work/golang-source" sparse-checkout set lang/golang
mv feeds/packages/lang/golang "$work/golang-sdk"
cp -a "$work/golang-source/lang/golang" feeds/packages/lang/golang
./scripts/feeds update -i packages
while read -r feed names; do
    [[ -z $feed || $feed == \#* ]] && continue
    if [[ $names == '*' ]]; then
        ./scripts/feeds install -f -a -p "$feed"
    else
        read -r -a selected <<< "$names"
        ./scripts/feeds install -f -p "$feed" "${selected[@]}"
    fi
done < "$root/feed-overrides.txt"
mapfile -t requested < <(python3 - "$root" "$device" <<'PY'
from pathlib import Path
import json, sys
root = Path(sys.argv[1])
single = json.loads((root / 'single-packages.json').read_text())
packages = {p for group in json.loads((root / 'package-groups.json').read_text()).values() for p in group}
for source in single.values():
    packages.update(source['dependencies'])
if sys.argv[2] == 'm28c':
    packages.update(s for s in (root / 'packages-m28c.txt').read_text().splitlines() if s and not s.startswith('#'))
print('\n'.join(sorted(packages - single.keys())))
PY
)
# Install requested applications and their dependencies, not thousands of
# unrelated SDK feed entries. Forced source owners above retain precedence.
./scripts/feeds install "${requested[@]}"
if [[ -d feeds/immortal_luci ]]; then
    ./scripts/feeds install -f -p immortal_luci luci-app-adguardhome
fi
python3 - "$root/single-packages.json" <<'PY'
from pathlib import Path
import json, sys
for name in json.load(open(sys.argv[1])):
    for link in Path('package/feeds').glob('*/' + name):
        if not link.is_symlink():
            raise SystemExit('Refusing to replace non-feed package: ' + str(link))
        link.unlink()
# The SDK already supplies its own kernel stub and module metadata.
kernel = Path('package/feeds/base/linux')
if kernel.is_symlink():
    kernel.unlink()
PY
# Generate metadata and check prerequisites once, before dispatching build jobs.
cat > .config <<'EOF'
# CONFIG_ALL is not set
# CONFIG_ALL_NONSHARED is not set
# CONFIG_ALL_KMODS is not set
CONFIG_SIGNED_PACKAGES=y
EOF
make defconfig
python3 - <<'PY'
import hashlib, json
from pathlib import Path
import subprocess
sources = {}
for feed in Path('feeds').iterdir():
    if (feed / '.git').exists():
        sources[feed.name] = subprocess.check_output(['git', '-C', str(feed), 'rev-parse', 'HEAD'], text=True).strip()
sources['golang'] = subprocess.check_output(['git', '-C', '../golang-source', 'rev-parse', 'HEAD'], text=True).strip()
for package in Path('package/custom').iterdir():
    sources[package.name] = subprocess.check_output(['git', '-C', str(package), 'rev-parse', 'HEAD'], text=True).strip()
patches = subprocess.check_output(['git', '-C', 'package/custom/luci-theme-design', 'diff'])
patches += subprocess.check_output(['git', '-C', 'feeds/packages', 'diff', '--', 'net/curl/Config.in', 'lang/rust/Makefile'])
sources['compatibility_patch'] = hashlib.sha256(patches).hexdigest()
Path('sources.json').write_text(json.dumps(sources, indent=2) + '\n')
PY

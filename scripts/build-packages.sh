#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
metadata=$(realpath "${1:?Usage: build-packages.sh metadata.json}")
device=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$metadata")
manager=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_manager"])' "$metadata")
group=${2:?Software group is required}
work="$root/.build/$device"
repo="$root/parts/$device/$group"
if [[ $group == openclash || $group == passwall ]]; then
    exec python3 "$root/scripts/fetch-prebuilt.py" "$metadata" "$group" "$repo" "$work/sdk/sources.json"
fi
mkdir -p "$repo"
cd "$work/sdk"
export GIT_AUTHOR_NAME='OpenWrt Build Bot' GIT_AUTHOR_EMAIL='bot@users.noreply.github.com'
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
mapfile -t packages < <(python3 - "$root" "$device" "$group" <<'PYGROUP'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
group = sys.argv[3]
packages = json.loads((root / 'package-groups.json').read_text())[group]
if sys.argv[2] == 'm28c' and group == 'platform':
    packages += [s for s in (root / 'packages-m28c.txt').read_text().splitlines() if s and not s.startswith('#')]
print('\n'.join(packages))
PYGROUP
)
# SDK defaults otherwise select every non-shared package in every added feed.
python3 - "${packages[@]}" <<'PY'
from pathlib import Path
import sys
p = Path('.config')
lines = [s for s in (p.read_text() if p.exists() else '').splitlines() if not s.startswith(('CONFIG_PACKAGE_', '# CONFIG_PACKAGE_', 'CONFIG_ALL', '# CONFIG_ALL', 'CONFIG_SIGN_EACH_PACKAGE', '# CONFIG_SIGN_EACH_PACKAGE', 'CONFIG_RUBY_ENABLE_YJIT', '# CONFIG_RUBY_ENABLE_YJIT'))]
lines += ['# CONFIG_ALL is not set', '# CONFIG_ALL_NONSHARED is not set', '# CONFIG_ALL_KMODS is not set',
          'CONFIG_SIGNED_PACKAGES=y', '# CONFIG_SIGN_EACH_PACKAGE is not set', 'CONFIG_LUCI_LANG_zh_Hans=y',
          '# CONFIG_RUBY_ENABLE_YJIT is not set']
# OpenClash uses Ruby for scripts/YAML; optional YJIT would build Rust and LLVM.
lines += ['CONFIG_PACKAGE_' + package + '=m' for package in sys.argv[1:]]
if 'luci-app-passwall' in sys.argv[1:]:
    lines += ['CONFIG_PACKAGE_luci-app-passwall_Nftables_Transparent_Proxy=y',
              '# CONFIG_PACKAGE_luci-app-passwall_Iptables_Transparent_Proxy is not set']
p.write_text('\n'.join(lines) + '\n')
PY
make defconfig
python3 - "${packages[@]}" <<'PY'
from pathlib import Path
import sys
config = set(Path('.config').read_text().splitlines())
missing = [p for p in sys.argv[1:] if not any(f'CONFIG_PACKAGE_{p}={s}' in config for s in ('y', 'm'))]
if missing:
    sys.exit('Unavailable packages for this SDK: ' + ', '.join(missing))
if 'CONFIG_RUBY_ENABLE_YJIT=y' in config:
    sys.exit('Ruby YJIT must be disabled to avoid an optional Rust toolchain build')
PY
if [[ $manager == apk ]]; then
    grep -qx 'CONFIG_USE_APK=y' .config
else
    ! grep -qx 'CONFIG_USE_APK=y' .config
fi
jobs=${BUILD_JOBS:-$(nproc)}
printf 'Building %s / %s with %s jobs; Ruby YJIT disabled\n' "$device" "$group" "$jobs"
printf 'Requested packages: %s\n' "${packages[*]}"
printf 'Downloading sources\n'
make download -j"$jobs" V=s BUILD_LOG=1
printf 'Compiling packages (including host toolchains); detailed logs: %s/logs\n' "$work/sdk"
if ! make -j"$jobs" V=s BUILD_LOG=1; then
    printf 'Parallel build failed; retrying with one job for diagnostics\n'
    make -j1 V=s BUILD_LOG=1
fi

# Collect this group's dependencies from every feed, not only the base feed.
# The publish job signs the combined repository after all groups succeed.
python3 - "$repo" "$manager" "${packages[@]}" <<'PY'
import hashlib
from pathlib import Path
import shutil, sys
dest = Path(sys.argv[1])
extension = 'apk' if sys.argv[2] == 'apk' else 'ipk'
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').digest()
for package in Path('bin').rglob('*.' + extension):
    output = dest / package.name
    if output.exists() and digest(output) != digest(package):
        sys.exit('Conflicting package: ' + package.name)
    shutil.copy2(package, output)
if not list(dest.glob('*.' + extension)):
    sys.exit('SDK produced no packages')
for name in sys.argv[3:]:
    separator = '-' if extension == 'apk' else '_'
    if not list(dest.glob(name + separator + '*.' + extension)):
        sys.exit('SDK did not produce requested package: ' + name)
PY
cp "$metadata" "$repo/build.json"
cp .config "$repo/sdk.config"
cp sources.json "$repo/sources.json"

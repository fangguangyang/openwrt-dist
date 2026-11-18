#!/usr/bin/env bash
set -euo pipefail
metadata=$(realpath "${1:?Usage: publish-feed.sh metadata.json directory repository}")
content=$(realpath "${2:?Repository directory required}")
repository=${3:?GitHub repository required}
[[ $repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
branch=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["feed_branch"])' "$metadata")
device=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$metadata")
git check-ref-format "refs/heads/$branch"
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/publish-feed.XXXXXXXX")
gh auth setup-git
git init "$work"
cd "$work"
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git remote add origin "https://github.com/$repository.git"
if git ls-remote --exit-code --heads origin "refs/heads/$branch" > /dev/null; then
    git fetch --depth 1 origin "$branch"
    git checkout -B publish FETCH_HEAD
else
    status=$?
    [[ $status == 2 ]] || exit "$status"
    git checkout --orphan publish
fi
# Preserve parent history so images pinned to earlier feed commits keep working.
git rm -r --ignore-unmatch . > /dev/null
cp -a "$content/." .
git add .
git commit --allow-empty -m "Build $branch ($GITHUB_RUN_ID/$GITHUB_RUN_ATTEMPT)"
git push origin "HEAD:refs/heads/$branch"
mkdir -p "$GITHUB_WORKSPACE/feed-results"
python3 - "$device" "$(git rev-parse HEAD)" "$GITHUB_WORKSPACE/feed-results/$device.json" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[3]).write_text(json.dumps({sys.argv[1]: sys.argv[2]}) + '\n')
PY

#!/bin/bash
# Package what an EC2 node runs: Duckstring and Trickle wheels built from a checkout, and this benchmark's code.
#
#     aws/build.sh /path/to/duckstring   # prints the code prefix to pass to launch.sh
#
# Uploads to s3://$BUCKET/code/{duckstring commit}/ (wheels/ and bench.tgz), so every run records exactly what it ran.
set -euo pipefail
BUCKET=${BUCKET:?set BUCKET to your results bucket}
REPO=$(cd "${1:?usage: build.sh /path/to/duckstring}" && pwd)
HERE=$(cd "$(dirname "$0")/.." && pwd)
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  COMMIT="$COMMIT-dirty"
fi
OUT=$(mktemp -d)
mkdir -p "$OUT/wheels"
(cd "$REPO" && uv build --wheel --out-dir "$OUT/wheels" packages/trickle >/dev/null 2>&1 && uv build --wheel --out-dir "$OUT/wheels" >/dev/null 2>&1)
tar -czf "$OUT/bench.tgz" -C "$HERE" --exclude __pycache__ --exclude results --exclude '*.json.gz' .
echo "{\"commit\": \"$COMMIT\", \"built\": \"$(date -u +%FT%TZ)\"}" > "$OUT/build.json"
aws s3 sync --quiet --delete "$OUT" "s3://$BUCKET/code/$COMMIT/"
rm -rf "$OUT"
echo "s3://$BUCKET/code/$COMMIT"

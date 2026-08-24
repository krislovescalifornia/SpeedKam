#!/usr/bin/env bash
# Pull the regression clips from the off-site fleet backup into this directory.
# Clips are NOT stored in git (privacy: real neighbourhood video). See README.md.
#
# Requires the `speedkam-web` SFTP alias (key auth; SFTP-only account) in ~/.ssh/config.
# Source: Pi-3 fleet bucket, CPU serial 000000002d47790c.
set -euo pipefail
cd "$(dirname "$0")"

REMOTE_BASE="public_html/website_489e3d33/fleet/speedkam_data/nodes/000000002d47790c/media"
BATCH="$(mktemp)"
trap 'rm -f "$BATCH"' EXIT

# Build one get-command per clip in labels.txt; date dir comes from the YYYYMMDD prefix.
while read -r clip _label; do
  [ -z "$clip" ] && continue
  ymd="${clip:0:8}"                       # 20260822
  date_dir="${ymd:0:4}-${ymd:4:2}-${ymd:6:2}"   # 2026-08-22
  echo "get ${REMOTE_BASE}/${date_dir}/${clip} ${clip}" >> "$BATCH"
done < labels.txt

echo "Pulling $(wc -l < "$BATCH") clips from the off-site backup..."
sftp -b "$BATCH" speedkam-web
echo "Done. $(ls -1 *.mp4 2>/dev/null | wc -l) clips present."

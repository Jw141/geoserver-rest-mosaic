#!/usr/bin/env bash
# Create the granule bucket and publish the fixtures.
#
# The bucket is made publicly readable on purpose: it lets GeoServer's HTTP
# range reader fetch granules over plain HTTP, which is the COG path this stack
# exercises by default.  This is a throwaway local emulator -- never model a
# real bucket policy on this.
set -euo pipefail

BUCKET="${MOSAIC_BUCKET:-mosaic-tiles}"

# The granules are mounted at /fixtures/tiles, mirroring their host path.
# /fixtures is also accepted so an older container, created when the mount
# landed one level up, still seeds instead of silently finding nothing.
CANDIDATES=(/fixtures/tiles /fixtures)

SRC=""
for dir in "${CANDIDATES[@]}"; do
    if compgen -G "$dir"/*.tif >/dev/null 2>&1; then
        SRC="$dir"
        break
    fi
done

awslocal s3api create-bucket --bucket "$BUCKET" >/dev/null 2>&1 || true

awslocal s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"PublicReadForRangeReader\",
    \"Effect\": \"Allow\",
    \"Principal\": \"*\",
    \"Action\": [\"s3:GetObject\"],
    \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"
  }]
}" >/dev/null

if [ -z "$SRC" ]; then
    echo "No .tif granules found in: ${CANDIDATES[*]}"
    echo "  Generate them on the host with 'make fixtures', then 'make seed-s3'."
    echo "  Contents of /fixtures:"
    ls -la /fixtures 2>/dev/null | sed 's/^/    /' || echo "    (/fixtures is not mounted)"
    exit 0
fi

# Granules go in the bucket root: the driver builds URLs as <base>/<filename>.
awslocal s3 sync "$SRC" "s3://${BUCKET}/" --exclude "*" --include "*.tif" >/dev/null
echo "Seeded s3://${BUCKET} from ${SRC} with $(ls -1 "$SRC"/*.tif | wc -l) granules"

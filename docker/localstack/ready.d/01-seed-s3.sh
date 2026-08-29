#!/usr/bin/env bash
# Create the granule bucket and publish the fixtures.
#
# The bucket is made publicly readable on purpose: it lets GeoServer's HTTP
# range reader fetch granules over plain HTTP, which is the COG path this stack
# exercises by default.  This is a throwaway local emulator -- never model a
# real bucket policy on this.
set -euo pipefail

BUCKET="${MOSAIC_BUCKET:-mosaic-tiles}"

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

if compgen -G "/fixtures/*.tif" >/dev/null; then
    awslocal s3 sync /fixtures "s3://${BUCKET}/" --exclude "*" --include "*.tif" >/dev/null
    echo "Seeded s3://${BUCKET} with $(ls -1 /fixtures/*.tif | wc -l) granules"
else
    echo "No fixtures found at /fixtures -- run 'make fixtures' then 'make seed-s3'"
fi

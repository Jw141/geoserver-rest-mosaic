CANDIDATES=(/home/jwilliams/projects/geoserver-rest-mosaic/fixtures/tiles)
SRC=""
for dir in "${CANDIDATES[@]}"; do
    if compgen -G "$dir"/*.tif >/dev/null 2>&1; then SRC="$dir"; break; fi
done
echo "  resolved SRC='${SRC:-<none>}'"

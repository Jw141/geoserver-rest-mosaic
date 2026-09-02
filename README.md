# geoserver-rest-mosaic

A Python client for GeoServer's REST API, focused on creating and maintaining
**ImageMosaic** layers. Targets **GeoServer 2.28.x and 3.0.x** from one code path.

- Workspace, coverage store and coverage creation, plus granule management
- **PostGIS** granule indexes (inline credentials, or a shared GeoServer store)
- **Remote COGs** — `http(s)://`, `s3://`, `gs://`, Azure — via the COG range readers
- **Local-to-GeoServer files** harvested by path, no upload needed
- **Uploaded files** for granules that live on the client machine
- Time dimensions driven by a filename regex

```bash
uv sync --extra dev --extra examples   # or: pip install -e '.[dev,examples]'
uv run pytest                          # 126 unit tests, no server needed
```

There is a full local test stack — GeoServer 2.28 **and** 3.0, PostGIS, and
LocalStack for remote COGs — plus a driver script that builds one mosaic per
granule location. See [Local test stack](#local-test-stack) below and
[docker/README.md](docker/README.md).

## Quick start — remote COGs on S3, indexed in PostGIS

```python
from geoserver_mosaic import (
    GeoServerClient, MosaicManager, MosaicDefinition,
    PostgisIndex, CogSettings, TimeRegex,
)

definition = MosaicDefinition(
    workspace="imagery",
    store="sentinel2",
    index=PostgisIndex(
        host="postgis.internal", database="gis",
        user="gis", password="secret", schema="mosaics",
    ),
    cog=CogSettings(range_reader="S3"),
    time_regex=TimeRegex(
        regex=r"[0-9]{8}T[0-9]{6}",
        date_format="yyyyMMdd'T'HHmmss",   # Java SimpleDateFormat
    ),
    granules=[
        "s3://tiles/S2_20240301T104021.tif",
        "s3://tiles/S2_20240302T104019.tif",
    ],
    srs="EPSG:32633",
    title="Sentinel-2 surface reflectance",
)

with GeoServerClient("https://geo.example.com/geoserver", "admin", "pw") as gs:
    result = MosaicManager(gs).create(definition)

print(result.layer)      # imagery:sentinel2
print(result.published)  # True
print(result.failed)     # [] — (location, message) pairs for bad granules
```

## The bootstrap sequence

An ImageMosaic cannot be configured from nothing, but granules on S3 or on the
GeoServer host cannot be uploaded through REST. `MosaicManager.create()`
therefore does this, which works for every granule location:

1. **Build a ZIP of just the `.properties` files** and `PUT` it to
   `file.imagemosaic?configure=none`. `CanBeEmpty=true` lets the store come up
   with an empty index; `configure=none` stops GeoServer publishing a layer
   before the coverage is configured properly.
2. **Harvest granules** by location. GeoServer has two endpoints here and they
   are *not* interchangeable — the client picks by URL scheme:

   | Endpoint | For | Sending the wrong thing |
   |---|---|---|
   | `external.imagemosaic` | Paths on the GeoServer host, incl. a directory to scan | A remote URL gives 400 "Failed to locate the input file" |
   | `remote.imagemosaic` | `http(s)://`, `s3://`, `gs://`, Azure URLs on a `Cog=true` mosaic | — |

   The first granule posted to `remote.imagemosaic` also initialises an empty
   mosaic, creating the index table and coverage.
3. **`POST` the coverage** explicitly, with dimensions and reader parameters.

The generated `indexer.properties` looks like this:

```properties
Name=sentinel2
Schema=*the_geom:Polygon,location:String,time:java.util.Date
TimeAttribute=time
AbsolutePath=true
CanBeEmpty=true
Caching=false
PropertyCollectors=TimestampFileNameExtractorSPI[timeregex](time)
Cog=true
CogRangeReader=it.geosolutions.imageioimpl.plugins.cog.S3RangeReader
```

## Granule locations

Three cases, distinguished by *where the file is*:

| Case | How to pass it | Notes |
|---|---|---|
| Remote COG | `granules=["s3://bucket/a.tif"]` | Requires `cog=CogSettings(...)`. Goes to `remote.imagemosaic`. |
| On the GeoServer host | `granules=["/data/tiles/a.tif"]` or `file:///data/tiles/`| Path resolved by GeoServer, not by this client. A directory is scanned recursively. Goes to `external.imagemosaic`. |
| On the client machine | `upload_files=[Path("a.tif")]` | Packed into the configuration ZIP |

They can be mixed in one definition.

## PostGIS indexing

Two options. Inline credentials write a `datastore.properties` into each store:

```python
index=PostgisIndex(host="db", database="gis", user="gis", password="secret")
```

Or reference a PostGIS store already registered in GeoServer, so credentials
live in one place and several mosaics share a connection pool:

```python
from geoserver_mosaic import ExistingIndexStore

gs.create_postgis_datastore(
    "imagery", "mosaic-index",
    host="db", database="gis", user="gis", password="secret",
)
index = ExistingIndexStore("imagery:mosaic-index")
```

To point several mosaics at one pre-existing granule table, set
`IndexerConfig.use_existing_schema=True`.

## Maintaining a mosaic

```python
mosaics = MosaicManager(gs)

# Add granules as new imagery lands.
harvested, failed = mosaics.add_granules(
    "imagery", "sentinel2", ["s3://tiles/S2_20240303T104018.tif"],
)

# Page through the index (mosaics can hold millions of granules).
for granule in gs.iter_granules("imagery", "sentinel2", "sentinel2"):
    print(granule["properties"]["location"])

# Drop old granules. purge=False leaves the files alone — always use that for
# remote granules you do not own.
mosaics.remove_granules(
    definition, filter="time BEFORE 2024-01-01T00:00:00Z", purge=False,
)

print(mosaics.describe(definition))
```

## Recreating a store

Deleting a coverage store does **not** remove its data directory, and for a
PostGIS index it does not drop the index table either. Recreating a store under
the same name inherits whatever survived, which can leave the mosaic unable to
initialise:

```
500 Failed to create reader from file:data/<workspace>/<store> and hints Hints:
```

`create(..., replace=True)` handles the index: it empties the granule index
before dropping the store, so a recreated mosaic does not start out holding
every granule of the old one — including granules whose files are long gone.
That silently inflates the granule count and looks like a successful build.

Two cases it cannot fix over REST:

- **A stale store directory** gives `Failed to create reader`. Use a fresh store
  name, or remove `<data_dir>/data/<workspace>/<store>` on the GeoServer host.
- **An orphaned index table** — the store was deleted by other means, leaving
  its table behind. The new mosaic adopts those rows. Drop the table, or use a
  fresh store name.

`purge` follows GeoServer's own vocabulary, `"none"` / `"metadata"` / `"all"`;
booleans are accepted and mapped. It is not a boolean on the wire — sending
`purge=false` is rejected with a bare 400. `purge="all"` deletes granule files,
so never use it on granules you need to keep.

## Empty mosaics

A mosaic with no granules is created but **not published** — GeoServer derives
the coverage's bands and envelope from the index and cannot describe an empty
one. `create()` logs a warning and returns `published=False`. Harvest at least
one granule, then call `publish_coverage(definition)`.

## Version compatibility

The REST surface used here is stable across 2.28 and 3.0, so compatibility is
handled by being tolerant rather than by branching:

- **No trailing slash on REST paths.** 3.0 routes `/rest/workspaces/` and
  `/rest/workspaces` differently and rejects the former; 2.x tolerated it. Every
  path is normalised in `client._clean_path`, unconditionally — 2.x is equally
  happy without the slash, so this needs no version branch. The rule applies to
  endpoint paths only: a directory location in a request body, such as
  `file:///data/tiles/`, keeps its trailing slash.
- Coverage bodies are sent as **XML**. GeoServer's JSON projection of
  `<metadata>`/`<parameters>` entries is ambiguous (one entry collapses to an
  object, several become a list) and has shifted between releases; the XML form
  has been stable for years.
- List responses are parsed through a helper that accepts an empty string, a
  single object, or a list for the same field.
- `/about/version` is read defensively and the GeoServer component is preferred
  over GeoTools/GeoWebCache.
- A major version above 3 is allowed with a logged warning rather than a hard
  failure.

Where a difference genuinely needs different behaviour per version, add a flag
to `compat.Features` and branch on that, rather than scattering version checks
through the client. Prefer a single tolerant code path where one exists — as
with the trailing-slash rule above.

```python
gs.version()            # Version(3, 0, 0)
gs.features.is_v3       # True
gs.features.supports_cog
```

## Server-side prerequisites

- **COG support** ships as four independent community packages, one per
  connection type: `cog-http-plugin` (plain HTTP), `cog-s3-plugin`,
  `cog-azure-plugin` and `cog-google-plugin`. Never install two cloud modules
  together — their dependencies conflict. Without the right one the store is
  created successfully but granule reads fail, a confusing failure mode worth
  checking first. The
  [Kartoza GeoServer image](https://hub.docker.com/r/kartoza/geoserver) installs
  them from `COMMUNITY_EXTENSIONS`, and `make check` reports each one.
- **The S3 reader is configured through `IIO_`-prefixed environment variables**
  (imageio-ext's namespace), *not* the AWS SDK's `AWS_ACCESS_KEY_ID` /
  `AWS_ENDPOINT_URL`, which it ignores. The segment after `IIO_` is the URL
  scheme, so `s3://bucket/key` granules read `IIO_S3_AWS_ENDPOINT`,
  `IIO_S3_AWS_USER`, `IIO_S3_AWS_PASSWORD` and `IIO_S3_AWS_REGION`. See
  [docker/README.md](docker/README.md#configuring-the-s3-reader).
- **PostGIS indexing** needs the PostGIS extension on the target database; the
  granule table is created automatically unless `use_existing_schema=True`.
- The GeoServer process needs read access to any path passed as a local granule
  location, and outbound network access for remote COGs.

## Local test stack

```bash
cp .env.example .env   # optional; everything has a default
make up                # fixtures + PostGIS + LocalStack + GeoServer 2.28 (:8080)
make ps                # wait for geoserver2 to report (healthy)
make smoke             # check + build all three mosaics + report
```

`make ui` prints the web UI URL and login — note GeoServer serves under the
`/geoserver` context path, so `http://localhost:8080` alone returns 404 while
`http://localhost:8080/geoserver/web` is the UI.

`make smoke` chains `check`, `driver` and `inspect`. `make mosaic WHICH=remote`
builds a single one. `make help` lists everything and shows the targeted server.

`make up-gs3` runs GeoServer 3.0 on :8081, and `make up-all` runs both side by
side — pointing the driver at each in turn is the compatibility check. Add
`GS=3` to any target to switch:

```bash
make smoke GS=3
```

[`examples/driver.py`](examples/driver.py) is the worked example as well as the
smoke test. It builds three mosaics, one per granule location:

| Command | Granules | Index |
|---|---|---|
| `driver.py local` | On the GeoServer host, harvested by directory | PostGIS |
| `driver.py remote` | COGs pulled from LocalStack S3 over HTTP | PostGIS |
| `driver.py upload` | Shipped from this machine in the config ZIP | Shapefile |

`make fixtures` generates the granules it uses: 12 real COGs — a 2x2 grid at 3
timestamps, tiled with overviews — so the mosaic genuinely stitches and the time
dimension genuinely has three instants. Each date is a different colour with
marker squares counting the time step, so the preview shows which slice you are
looking at.

Generation is **additive** — nothing is deleted without `--prune`.
`make fixtures-add DATES=2` appends two more dates after the newest existing
one, so you can watch the mosaic grow across re-harvests.

`make fixtures-scatter SCATTER=8 DATES=4` places granules at random points
instead of tiling a grid, giving a sparse mosaic with gaps and overlaps spread
across a wide extent. Placement is seeded per date, so it is reproducible.

**The one gotcha**: the driver runs on your machine, but the granule URLs and
database host it sends are resolved by *GeoServer*, inside the compose network.
They default to compose service names (`postgis`, `localstack`) for that reason.
Using `localhost` there creates the store fine and then fails every harvest.
[docker/README.md](docker/README.md) covers this and the rest of the
troubleshooting.

Integration tests run against a live stack and assert what unit tests cannot —
that GeoServer accepts the config, indexes the granules, and renders the mosaic
through WMS:

```bash
make integration
```

## Error handling

```python
from geoserver_mosaic import (
    GeoServerHTTPError, NotFoundError, ConflictError,
    AuthenticationError, MosaicConfigurationError,
)
```

`MosaicConfigurationError` is raised **before any HTTP traffic** for
configurations that would fail confusingly on the server — a `TimeAttribute`
missing from the index schema, remote granules without `CogSettings`, a COG
mosaic with `absolute_path=False`.

`GeoServerHTTPError.message` extracts a readable line from GeoServer's error
bodies, which may be an HTML page, a bare line, or a Java stack trace.

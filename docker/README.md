# Local test stack

Three services, defined in the repo-root [`docker-compose.yml`](../docker-compose.yml):

| Service | Purpose | Host port |
|---|---|---|
| `postgis` | Granule index database | 5432 |
| `localstack` | S3 emulator holding remote COG granules (pinned token-free) | 4566 |
| `geoserver2` | `kartoza/geoserver:2.28.4` (profile `gs2`) | 8080 |
| `geoserver3` | `kartoza/geoserver:3.0.0` (profile `gs3`) | 8081 |

GeoServer is the **Kartoza** image, which can pull the COG community plugins at
startup, requested through `COMMUNITY_EXTENSIONS`. COG ships as four independent
packages, one per connection type:

| Package | For |
|---|---|
| `cog-http-plugin` | Plain HTTP(S), no particular cloud target |
| `cog-s3-plugin` | AWS S3 and S3-compatible stores |
| `cog-azure-plugin` | Azure Blob Storage |
| `cog-google-plugin` | Google Cloud Storage |

This stack installs `cog-http-plugin,cog-s3-plugin`. **Never install two cloud
providers together** — GeoServer's docs warn that the AWS, Google and Azure
modules' dependencies interfere with each other. `cog-http-plugin` is not a
cloud module and coexists with any one of them.

`postgis` and `localstack` carry no profile, so they start alongside whichever
GeoServer you select. Both GeoServers can run at once (`--profile all`), which
is the point: the same driver run against both ports is the compatibility test.

## Getting started

```bash
cp .env.example .env   # optional; every value has a default
make up                # fixtures + PostGIS + LocalStack + GeoServer 2.28
make ps                # wait until geoserver2 shows (healthy)
make smoke             # check + build all three mosaics + report
```

`make smoke` is the usual run — it chains `check`, `driver` and `inspect`. The
pieces individually:

| Target | Does |
|---|---|
| `make check` | Server version, and which COG plugins actually installed |
| `make driver` | Builds all three demo mosaics, recreating any that exist |
| `make mosaic WHICH=remote` | Builds just one (`local`, `remote` or `upload`) |
| `make inspect` | Granule counts and WMS preview URLs for what exists |
| `make integration` | The pytest integration suite against the running stack |
| `make clean-gs` | Deletes the demo workspace, leaving the containers up |

Every one of those takes `GS=3` to target the 3.0 container instead, or
`URL=https://host/geoserver` to point anywhere:

```bash
make smoke GS=3
make mosaic WHICH=remote GS=3
```

`make help` prints the list and the currently targeted server.

First boot is slow — Kartoza downloads and installs the community plugins
before serving. `make logs` shows progress; the healthcheck allows 4 minutes.

Note the admin password: Kartoza's own default is `myawesomegeoserver`, but the
compose file sets it to `geoserver` (via `GEOSERVER_PASSWORD`) so it matches the
driver's default.

Against 3.0 instead:

```bash
make up-gs3
make smoke GS=3
```

Running `make up-all` starts both, and running the driver against each port in
turn is the compatibility check this stack exists for.

## Reaching the web UI

```bash
make ui          # prints the URL, the login, and whether it answers
make ui GS=3     # the 3.0 container instead
```

GeoServer is served under the **`/geoserver` context path**, so the bare
host:port returns 404 and looks like the port was never published:

| URL | |
|---|---|
| `http://localhost:8080` | 404 — no app at the root |
| `http://localhost:8080/geoserver/web` | the web UI |

Login is `admin` / `geoserver` (set by `GEOSERVER_PASSWORD`; Kartoza's own
default, `myawesomegeoserver`, is overridden in the compose file).

The ports are published by `docker-compose.yml` already — 8080 for 2.28, 8081
for 3.0, both bound on all interfaces. On Docker Desktop with WSL2, `localhost`
normally works from a Windows browser; if it does not, `make ui` also prints the
distro's IP address to use instead.

## The hostname split

This trips people up. The driver runs on **your machine**, but the granule URLs
and index connection details it sends are resolved by **GeoServer**, inside the
compose network. So:

| Setting | Value | Resolved by |
|---|---|---|
| `GEOSERVER_URL` | `http://localhost:8080/geoserver` | your shell |
| `PG_HOST_FROM_GEOSERVER` | `postgis` | GeoServer |
| `S3_BASE_FROM_GEOSERVER` | `http://localstack:4566/mosaic-tiles` | GeoServer |
| `GRANULE_DIR_IN_GEOSERVER` | `/opt/granules` | GeoServer |

Putting `localhost` in the last three is the classic failure: the store is
created successfully and every granule then fails to harvest, because GeoServer
resolves `localhost` to its own container.

## Granules

`make fixtures` writes 12 real COGs to `fixtures/tiles/` — a 2x2 spatial grid at
3 timestamps, tiled with overviews, filenames like
`S2_20240301T104021_r0c0.tif`. Each date gets its own colour plus a run of
marker squares along the top edge, one per time step, so a WMS preview at
different `time=` values is unmistakably different.

### Layouts

`--grid` (default) tiles the extent neatly. `--scatter N` places N granules per
date at random points instead, so the mosaic is sparse and spread out — gaps,
overlaps and empty space, which exercises the reader far more than a tidy
tiling:

```bash
make fixtures-scatter SCATTER=8 DATES=4        # 8 granules per date, 4 dates
make fixtures-scatter SCATTER=12 DATES=6 EXTENT="0 35 30 60"
```

Placement is seeded per date, so a date always lands in the same places however
often it is regenerated, and appended dates land somewhere new rather than
retracing the first batch. `--seed` picks a different arrangement, `--tile-deg`
sets granule size, and `--extent W S E N` the region they scatter within.

Both the generator and the driver report the covered extent, and the driver's
preview URLs use the layer's own bounding box — a hardcoded one would frame
scattered granules on empty space.

**More granules do not make a bigger picture.** Appending dates adds *time
steps*, not area — the mosaic stays a 2x2 grid covering the same extent, and the
preview shows **one slice at a time** (the newest, per the `MAXIMUM` default
strategy). 24 files is 6 dates x 4 tiles, and each preview shows 4 of them. To
see the others, pass `time=` — the driver prints a URL per time step, and the
layer preview has a time selector. The marker squares along the top edge count
the time step, so you can tell slices apart at a glance.

**Generation is additive.** Nothing is deleted unless you pass `--prune`; delete
files yourself when you are ready. To watch the mosaic grow, append time steps
and re-harvest:

```bash
make fixtures-add DATES=2     # two more dates after the newest existing one
make seed-s3                  # push them to LocalStack
make mosaic WHICH=remote      # re-harvest, then reload the WMS preview
```

Each run reports what changed:

```
  new: 8   overwritten: 12   kept: 0
  directory now holds 20 granule(s), 2656 KiB
  time steps: 2024-03-01, 2024-03-06, 2024-03-11, 2024-03-16, 2024-03-21
```

That directory is:

- mounted read-only into GeoServer at `/opt/granules` (the "local" mosaic),
- synced into the LocalStack bucket (the "remote" mosaic),
- read directly by the driver for the "upload" mosaic.

Regenerating them needs a re-sync: `make seed-s3`.

The granules are mounted into LocalStack at `/fixtures/tiles`, mirroring their
host path so the two cannot drift. If `make seed-s3` reports finding nothing,
the container predates that mount — recreate it:

```bash
docker compose up -d --force-recreate localstack
```

The seed script also accepts `/fixtures` so an older container still seeds
rather than silently doing nothing, and lists the directory when it finds no
`.tif` files at all.

`make verify-s3` confirms LocalStack serves them the way the COG HTTP reader
needs — an anonymous HTTP range request returning `206`. Worth running before
blaming GeoServer, since it tests the bucket policy and range support without
GeoServer in the picture at all.

## HTTP and S3 range readers

Both are available, because the image installs `cog-http-plugin` and
`cog-s3-plugin`. The driver defaults to HTTP and takes `--range-reader S3` to
switch.

**HTTP** (default) fetches from `http://localstack:4566/mosaic-tiles/...`. The
seed script makes the bucket publicly readable, so no credentials are involved.
Fewest moving parts, so it is what the integration tests cover.

```bash
uv run python examples/driver.py remote
```

**S3** uses `s3://mosaic-tiles/...` and imageio-ext's own S3 client — which has
its own configuration namespace, covered below:

```bash
uv run python examples/driver.py remote --range-reader S3 --replace
```

### Running S3 only

`cog-s3-plugin` is self-contained — it does **not** need `cog-http-plugin` as a
base — so if `s3://` is all you use, that one line is enough:

```bash
GS_COMMUNITY_EXTENSIONS=cog-s3-plugin
COG_RANGE_READER=S3
```

`COG_RANGE_READER` makes the driver's `remote` mosaic use `s3://` URLs by
default, matching the plugin set.

For the **test stack specifically** I'd still keep `cog-http-plugin` installed
alongside it. It is not a cloud module, so it conflicts with nothing, and it
gives you a known-good control: if `s3://` granules fail to harvest, re-running
with `--range-reader HTTP` separates "COG is broken" from "my S3 endpoint
configuration is wrong". That distinction is otherwise hard to make, because
both failures surface as the same harvest error. On a production GeoServer,
install only what you use.

### Configuring the S3 reader

The range reader is **imageio-ext's**, not the AWS SDK's default credential
chain, so it reads `IIO_`-prefixed variables and ignores `AWS_ACCESS_KEY_ID`,
`AWS_ENDPOINT_URL` and friends entirely. The compose file sets:

| Variable | Value here | Purpose |
|---|---|---|
| `IIO_S3_AWS_ENDPOINT` | `http://localstack:4566` | Points at LocalStack instead of real AWS |
| `IIO_S3_AWS_USER` | `test` | Access key ID |
| `IIO_S3_AWS_PASSWORD` | `test` | Secret access key |
| `IIO_S3_AWS_REGION` | `us-east-1` | Region |
| `IIO_S3_AWS_FORCE_PATH_STYLE` | `true` | Path-style addressing — required by LocalStack and MinIO, defaults to `false` upstream |

The segment after `IIO_` is the **URL scheme**, not a fixed string. Granules
addressed as `s3://bucket/key` are configured by `IIO_S3_*`. If you register a
custom scheme for a non-AWS endpoint — `myminio://bucket/key` — that same
granule is configured by `IIO_MYMINIO_AWS_*` instead, which is how you point
different mosaics at different object stores in one GeoServer.

`IIO_S3_AWS_ENDPOINT` is the load-bearing one: without it the reader goes to
real AWS, and harvesting fails with a credentials or not-found error that says
nothing about LocalStack.

Two more, global rather than per-scheme, set in the compose file and tunable
from `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `IIO_HTTP_MAX_REQUESTS` | 128 | Total concurrent range requests |
| `IIO_HTTP_MAX_REQUESTS_PER_HOST` | 16 | Per-host cap — the one that bites when every granule is on one bucket |

For a COG whose header does not fit in the default 16 KB initial read — many
tiles, or many overview levels — raise the chunk size. That one is a Java system
property rather than an environment variable:
`it.geosolutions.cog.default.header.length`.

Any `IIO_` variable can be given as a system property instead by lowercasing and
swapping underscores for dots (`IIO_HTTP_MAX_REQUESTS` →
`iio.http.max.requests`). Environment variables are checked first.

Source: [COG support, GeoServer user manual](https://docs.geoserver.org/main/en/user/community/cog/cog/).

## Troubleshooting

**A COG plugin did not install.** `make check` reports each one separately:

```
  gs-cog-http installed (gs-cog-http-2.28.4)
  gs-cog-s3  NOT INSTALLED -- s3:// granule URLs will fail
```

This matters because a missing COG module is a **silent** failure at store
creation time — the store is created happily, and reads fail later.

Kartoza downloads community plugins at startup and they are not published for
every GeoServer version, so a plugin can simply be unavailable for the tag you
pinned. Check the startup logs for the download, adjust
`GS_COMMUNITY_EXTENSIONS` in `.env`, and recreate:

```bash
docker compose --profile gs2 up -d --force-recreate geoserver2
```

Swap in `cog-azure-plugin` or `cog-google-plugin` for those providers — one
cloud module at a time, per the warning above.

**LocalStack asks for an auth token.** Images published from **2026-03-23**
require a `LOCALSTACK_AUTH_TOKEN` even for community services like S3. The
compose file pins `localstack/localstack:3`, which predates that and runs
token-free — pinning is LocalStack's own documented way to avoid the
requirement, and this tag is verified working here. Symptoms if you move the pin forward without a token: the container
exits or refuses requests at startup, and because `geoserver*` waits on
`localstack` being healthy, GeoServer never starts either — so it looks like a
GeoServer problem. Check the actual cause first:

```bash
docker compose logs localstack | head -30
curl -s http://localhost:4566/_localstack/health | head
```

To use a newer image, get a token (the free tier issues them at
[app.localstack.cloud](https://app.localstack.cloud)) and set both in `.env`:

```bash
LOCALSTACK_VERSION=latest
LOCALSTACK_AUTH_TOKEN=ls-...
```

The token is passed through only when set, so leaving it commented out keeps the
default run offline and anonymous.

**The image tag does not exist.** Kartoza tags track GeoServer releases but not
instantly, and 3.0.0 in particular may not be published yet. Check Docker Hub
for `kartoza/geoserver` and set `GS2_VERSION` / `GS3_VERSION` accordingly — the
defaults here are the versions this library targets, not verified tags.

**`500 Failed to create reader from file:data/...`.** The store's data
directory survived a previous delete and is stale. Use a fresh store name, or
remove `<data_dir>/data/<workspace>/<store>` in the container and drop the
PostGIS index table:

```bash
docker compose exec geoserver2 rm -rf /opt/geoserver/data_dir/data/mosaic-demo/tiles-remote
docker compose exec postgis psql -U gis -d gis -c 'drop table if exists "tiles-remote"'
```

`make clean-volumes` resets everything if you would rather start over.

**s3:// granules fail with "Failed to create reader" while http:// ones work.**
Check `IIO_S3_AWS_FORCE_PATH_STYLE=true`. LocalStack and MinIO address buckets
by path (`host/bucket/key`) while the AWS SDK defaults to virtual-host style
(`bucket.host/key`), which never resolves against them. It defaults to `false`
upstream, so `IIO_S3_AWS_ENDPOINT` alone is not enough. Env vars are read at
JVM startup, so recreate the container after changing it:

```bash
docker compose --profile gs2 up -d --force-recreate geoserver2
```

**Store creates, every granule fails to harvest.** Almost always the hostname
split above. Check `docker compose logs geoserver2` for the resolution error.

**`s3://` granules fail while `http://` ones work.** Check `IIO_S3_AWS_ENDPOINT`
is set and reaches LocalStack. Setting `AWS_ENDPOINT_URL` instead does nothing —
the COG reader does not read the AWS SDK's variables — and the reader then talks
to real AWS, so the error mentions credentials or a missing bucket rather than
anything about your endpoint.

**Mosaic exists but WMS returns a blank or exception.** Confirm the granules are
indexed (`make inspect`) and that their CRS matches the coverage `srs`. The
demo fixtures are all EPSG:4326.

## Resetting

```bash
make clean-gs        # delete just the demo workspace, keep the containers
make down            # stop, keep the catalog and database
make clean-volumes   # stop and delete everything, for a truly clean run
```

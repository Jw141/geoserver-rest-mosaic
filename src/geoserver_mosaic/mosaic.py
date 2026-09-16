"""High-level ImageMosaic creation.

The bootstrap problem this module solves: an ImageMosaic store cannot be
configured from nothing, but the granules you want in it may live on S3 or on
the GeoServer host, where they cannot be uploaded through the REST API.  The
sequence that works for every granule location is:

1. Build a ZIP holding only the mosaic's ``.properties`` files and PUT it to
   ``file.imagemosaic?configure=none``.  ``CanBeEmpty=true`` in the indexer
   lets the store come up with an empty index, and ``configure=none`` stops
   GeoServer publishing a layer before the config is complete.
2. Harvest granules by location, whichever scheme they use.
3. POST the coverage explicitly, with dimensions and reader parameters.

Doing it in that order means the same code path serves uploaded files, files
already on the GeoServer host, and remote COGs.

There is a second way to stand up a store, for a mosaic directory that already
exists on the GeoServer host complete with its own ``indexer.properties`` (or
that GeoServer may write one into): register the directory itself with
``external.imagemosaic``.  :attr:`MosaicDefinition.location` selects that
mode.  It is the right choice when the configuration is provisioned outside
this client, and the wrong one when it is not -- nothing in the definition can
be delivered to a directory outside GeoServer's data directory over REST.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from typing import Any

from . import payloads, properties
from .client import GeoServerClient, host_path
from .errors import (
    GeoServerError,
    GeoServerHTTPError,
    MosaicConfigurationError,
    NotFoundError,
)
from .models import (
    CogSettings,
    DimensionInfo,
    ExistingIndexStore,
    IndexerConfig,
    IndexStore,
    PostgisIndex,
    ShapefileIndex,
    TimeRegex,
    is_remote_location,
    time_dimension,
    timestamp_collector,
)

log = logging.getLogger("geoserver_mosaic")

#: Reader parameters that are almost always right for a tiled mosaic.  Callers
#: can override or extend them through ``MosaicDefinition.parameters``.
DEFAULT_COVERAGE_PARAMETERS: dict[str, Any] = {
    "AllowMultithreading": True,
    "USE_JAI_IMAGEREAD": False,
    "SUGGESTED_TILE_SIZE": "512,512",
}


@dataclass
class MosaicDefinition:
    """Everything needed to stand up one ImageMosaic layer.

    Two ways to create the store, chosen by :attr:`location`:

    * **Configured here** (``location=None``, the default).  The index, COG
      and time settings below are rendered into ``.properties`` files and
      uploaded; granules are then harvested from :attr:`granules` and
      :attr:`upload_files`.  Works for remote COGs, files on the GeoServer
      host, and files on this machine.
    * **Pre-provisioned directory** (``location="file:///data/mosaic/"``).
      The store is registered on a directory that already exists on the
      GeoServer host and is configured by whatever ``.properties`` files it
      holds.  Only the layer metadata below applies; the configuration fields
      must be left unset because they cannot reach that directory over REST.
    """

    workspace: str
    store: str

    #: Directory on the GeoServer host to root the store at, as an absolute
    #: path (a ``file:`` URL is reduced to one).  Selects the pre-provisioned
    #: mode described above.
    location: str | None = None

    #: Granule index backend.  PostGIS is strongly preferred for anything that
    #: will be harvested over time.
    index: IndexStore = field(default_factory=ShapefileIndex)

    #: Published layer name.  Defaults to the store name.
    coverage: str | None = None

    #: Mosaic name written to ``indexer.properties``.  For a PostGIS index this
    #: is also the granule table name.  Defaults to the store name.  In the
    #: pre-provisioned mode it names the coverage inside the directory's own
    #: configuration, and is discovered from the server when left unset.
    mosaic_name: str | None = None

    #: Remote COG access.  Leave as ``None`` for plain local granules.
    cog: CogSettings | None = None

    #: Timestamp extraction from granule filenames.
    time_regex: TimeRegex | None = None

    #: Set explicitly to override the derived indexer; otherwise one is built
    #: from ``mosaic_name``, ``time_regex`` and ``cog``.
    indexer: IndexerConfig | None = None

    #: Locations to harvest after the store exists: remote URLs, or absolute
    #: paths / ``file:`` URLs that the GeoServer process can resolve.
    granules: Sequence[str] = ()

    #: Local files uploaded inside the configuration ZIP.  Only for granules on
    #: the machine running this client, not on the GeoServer host.
    upload_files: Sequence[Path] = ()

    #: Layer metadata.
    srs: str = "EPSG:4326"
    title: str | None = None
    abstract: str | None = None
    keywords: Sequence[str] = ()
    default_style: str | None = None

    #: WMS dimensions.  A time dimension is added automatically when the
    #: indexer has a time attribute and this is left empty.
    dimensions: dict[str, DimensionInfo] = field(default_factory=dict)

    #: Coverage reader parameters, merged over :data:`DEFAULT_COVERAGE_PARAMETERS`.
    parameters: dict[str, Any] = field(default_factory=dict)

    #: Extra files placed in the store directory alongside the properties
    #: files, e.g. a custom ``*.sld`` or an auxiliary regex.
    extra_files: dict[str, str] = field(default_factory=dict)

    @property
    def is_external(self) -> bool:
        """True in the pre-provisioned directory mode."""
        return self.location is not None

    def resolved_coverage(self) -> str:
        return self.coverage or self.store

    def resolved_mosaic_name(self) -> str:
        return self.mosaic_name or self.store

    def resolved_indexer(self) -> IndexerConfig:
        """Build the indexer if one was not supplied explicitly."""
        if self.indexer is not None:
            indexer = self.indexer
        else:
            collectors: list[str] = []
            time_attribute: str | None = None
            if self.time_regex is not None:
                time_attribute = "time"
                regex_stem = Path(self.time_regex.filename).stem
                collectors.append(timestamp_collector(time_attribute, regex_stem))
            indexer = IndexerConfig(
                name=self.resolved_mosaic_name(),
                time_attribute=time_attribute,
                property_collectors=collectors,
                mosaic_crs=self.srs,
            )
            if time_attribute is None:
                # Drop the time column from the default schema so the index
                # table matches what the collectors actually populate.
                indexer.schema = "*the_geom:Polygon,location:String"
        indexer.validate()
        return indexer

    def resolved_dimensions(self) -> dict[str, DimensionInfo]:
        if self.dimensions:
            return dict(self.dimensions)
        if self.is_external:
            # The directory's own indexer decides the dimensions, and it is not
            # readable from here; callers declare them explicitly.
            return {}
        indexer = self.resolved_indexer()
        dimensions: dict[str, DimensionInfo] = {}
        if indexer.time_attribute:
            dimensions["time"] = time_dimension()
        if indexer.elevation_attribute:
            dimensions["elevation"] = DimensionInfo(units="EPSG:5030")
        return dimensions

    def validate(self) -> None:
        """Reject configurations that would fail confusingly on the server."""
        if self.is_external:
            self._validate_external()
            return
        indexer = self.resolved_indexer()
        if self.cog is not None and not indexer.absolute_path:
            raise MosaicConfigurationError(
                "COG granules are addressed by URL, so the indexer must set "
                "absolute_path=True"
            )
        if indexer.time_attribute and self.time_regex is None and not indexer.property_collectors:
            raise MosaicConfigurationError(
                f"Indexer declares TimeAttribute {indexer.time_attribute!r} but "
                "no time_regex or property collector was given, so nothing "
                "would populate it"
            )
        _check_files_exist(self.upload_files)
        remote = [g for g in self.granules if is_remote_location(g)]
        if remote and self.cog is None:
            raise MosaicConfigurationError(
                f"Granule locations {remote[:3]} are remote URLs but no "
                "CogSettings were provided; the mosaic reader cannot open them"
            )

    def _validate_external(self) -> None:
        """A pre-provisioned directory brings its own configuration.

        Anything that would render into a ``.properties`` file is rejected
        rather than silently dropped, because the directory lives outside
        GeoServer's data directory and REST cannot write to it.
        """
        undeliverable = {
            "index": not isinstance(self.index, ShapefileIndex),
            "cog": self.cog is not None,
            "time_regex": self.time_regex is not None,
            "indexer": self.indexer is not None,
            "upload_files": bool(self.upload_files),
            "extra_files": bool(self.extra_files),
        }
        offending = [name for name, set_ in undeliverable.items() if set_]
        if offending:
            raise MosaicConfigurationError(
                f"location={self.location!r} registers a directory that carries "
                f"its own configuration, so {offending} cannot be applied; put "
                "the corresponding files in the directory instead, or drop "
                "'location' and let this client configure the store"
            )
        if is_remote_location(self.location or ""):
            raise MosaicConfigurationError(
                f"location={self.location!r} must be a path on the GeoServer "
                "host; remote granules go in 'granules' with CogSettings"
            )


def _check_files_exist(files: Iterable[Path]) -> None:
    missing = [str(p) for p in files if not Path(p).is_file()]
    if missing:
        raise MosaicConfigurationError(f"upload_files not found: {missing}")


def build_config_archive(definition: MosaicDefinition) -> bytes:
    """Render the mosaic's configuration files into an in-memory ZIP.

    Returns the bytes to PUT at ``file.imagemosaic`` to *create* the store.
    Uploaded granules ride along in the same archive, since GeoServer indexes
    whatever it unpacks.
    """
    indexer = definition.resolved_indexer()
    indexer_props = indexer.to_properties()
    if definition.cog is not None:
        indexer_props.update(definition.cog.to_properties())

    members: dict[str, str] = {
        "indexer.properties": properties.dumps(
            indexer_props, header="Generated by geoserver-rest-mosaic"
        )
    }

    index_props = definition.index.to_properties()
    if index_props:
        members["datastore.properties"] = properties.dumps(index_props)

    if definition.time_regex is not None:
        members[definition.time_regex.filename] = properties.dumps(
            definition.time_regex.to_properties()
        )

    members.update(definition.extra_files)
    return _zip(members, definition.upload_files)


def build_granule_archive(files: Iterable[Path]) -> bytes:
    """Zip local granule files, and nothing else, for harvesting.

    PUT to ``file.imagemosaic`` on an *existing* mosaic store, GeoServer treats
    the archive as granules to index.  No configuration files are included:
    they would be ignored there anyway, and their absence makes the intent
    unambiguous.
    """
    return _zip({}, files)


def _zip(members: dict[str, str], files: Iterable[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
        for path in files:
            archive.write(path, arcname=Path(path).name)
    return buffer.getvalue()


@dataclass
class MosaicResult:
    """Outcome of a :meth:`MosaicManager.create` call."""

    workspace: str
    store: str
    coverage: str
    #: True when the coverage (and therefore the WMS/WCS layer) exists.
    published: bool
    #: True when this call created the store.  False means it already existed
    #: and was harvested into, with its configuration left as it was.
    created: bool = True
    #: Locations successfully harvested into the index.
    harvested: list[str] = field(default_factory=list)
    #: ``(location, message)`` for granules that could not be harvested.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def layer(self) -> str:
        """Qualified layer name, as used in WMS requests."""
        return f"{self.workspace}:{self.coverage}"

    @property
    def ok(self) -> bool:
        return self.published and not self.failed


class MosaicManager:
    """Mosaic-oriented operations on top of :class:`GeoServerClient`."""

    def __init__(self, client: GeoServerClient) -> None:
        self.client = client

    # -- creation ----------------------------------------------------------

    def create(
        self,
        definition: MosaicDefinition,
        *,
        create_workspace: bool = True,
        publish: bool = True,
        replace: bool = False,
        purge: str | bool | None = None,
        verify: bool = True,
    ) -> "MosaicResult":
        """Create the store, harvest granules and publish the coverage.

        GeoServer acknowledges every harvest with ``202`` and an empty body,
        whether or not it could open the granule -- a bad key, an unreachable
        bucket or a misconfigured range reader all look like success.  With
        ``verify`` (the default) the index is consulted after publishing and
        anything that did not land is moved from ``harvested`` to ``failed``;
        see :meth:`verify_granules`.

        The call is safe to repeat.  When the store already exists its
        configuration is **kept, not re-applied**: GeoServer has no REST route
        that reconfigures an ImageMosaic in place, and uploading the config
        ZIP again would be interpreted as a batch of granules to harvest.
        What does happen on a repeat is that ``granules`` and ``upload_files``
        are harvested (the mosaic reader skips locations it already holds) and
        the coverage's metadata is updated.  ``MosaicResult.created`` says
        which path was taken.

        ``replace=True`` deletes an existing store of the same name first, and
        is the only way to change index, COG or regex settings.  For a
        pre-provisioned directory (``location`` set) it re-registers the store
        and leaves the directory alone.

        What ``replace`` leaves in place is as deliberate as what it removes,
        because GeoServer keeps two things beyond the catalog entry and the
        REST API can only partly reach them:

        * **The store's directory** (``data/<workspace>/<store>``) of a
          PostGIS-indexed mosaic holds the mosaic configuration GeoServer
          derived at first use (``<name>.properties``).  That file outranks a
          freshly uploaded ``indexer.properties``, so a mosaic switched from
          the HTTP to the S3 range reader would silently keep using HTTP.  It
          is also what lets a re-created store work over its old index table
          at all: without it the reader tries to create the table, finds it,
          and never indexes anything.  So the directory is kept, and the
          file is patched in place through the resource API with this
          definition's settings before the store is created again.  (A
          shapefile-indexed mosaic keeps its index *in* that directory, and
          an emptied shapefile index cannot be harvested into again, so there
          the directory is removed and the store starts from scratch.)
        * **The PostGIS index table** cannot be dropped from here.  The only
          REST route, ``purge``, makes GeoServer attempt to drop the whole
          database (its usual ``500 Unable to drop the database`` is that
          attempt failing while other connections exist; when it succeeds
          every table goes).  So ``purge`` is never sent unless you pass it,
          and you should not.  Instead the index is emptied row by row
          before the store is deleted, and the new store harvests into the
          same table.  To change the index *schema*, drop the table in the
          database or use a new ``mosaic_name``.

        A store deleted by other means leaves the same two things behind.
        ``create`` copes: a leftover directory with a configuration is
        patched and reused, its stale rows pruned once the new granules are
        verified (when every one of them names a single granule; a directory
        harvest cannot be reconciled and keeps them, with a warning).  A
        leftover table *without* a directory is the hard case -- the fresh
        store is dead on arrival -- and is handled once by bootstrapping a
        configuration from the table's rows (a store with
        ``UseExistingSchema=true``, which can be published but not harvested
        into) and then building over it.  An empty leftover table cannot be
        recovered over REST at all; drop it.

        Publishing is skipped when the mosaic ends up with no granules, since
        GeoServer derives the coverage's bands and envelope from the index and
        cannot describe an empty one.
        """
        definition.validate()
        workspace = definition.workspace
        store = definition.store

        if create_workspace:
            self.client.create_workspace(workspace, exist_ok=True)

        reused_index = False
        index_cleared = False
        existed = self.client.coverage_store_exists(workspace, store)
        if existed and replace:
            log.info("Replacing existing store %s:%s", workspace, store)
            keeps = _keeps_directory(definition)
            if keeps:
                index_cleared = self._empty_indexes(workspace, store)
            # A table-backed mosaic keeps its directory (see the docstring); a
            # shapefile-backed one has its index *in* the directory, and an
            # emptied shapefile index cannot be harvested into again, so that
            # directory goes and the store starts from scratch.
            self._delete_store(
                workspace, store, purge, remove_directory=not keeps and not definition.is_external
            )
            existed = False

        if existed:
            log.info(
                "Store %s:%s already exists; keeping its configuration and "
                "harvesting into it (use replace=True to reconfigure)",
                workspace,
                store,
            )
            if definition.upload_files:
                self.add_files(workspace, store, definition.upload_files)
        else:
            reused_index = self._create_store(definition)
            if reused_index and definition.upload_files:
                # A store re-created over its old configuration does not walk
                # its directory again, so files unpacked from the archive are
                # sent once more as a harvest.
                self.add_files(workspace, store, definition.upload_files)

        harvested, failed = self.add_granules(workspace, store, definition.granules)

        coverage_name = definition.resolved_coverage()
        published = False
        if publish:
            if self._can_publish(definition, harvested, existed):
                try:
                    self.publish_coverage(definition)
                except GeoServerHTTPError as exc:
                    if existed or not self._orphaned_index_suspected(definition, exc):
                        raise
                    harvested, failed = self._rebuild_over_orphaned_index(definition, exc)
                    reused_index, index_cleared = True, False
                published = True
            else:
                log.warning(
                    "Not publishing %s:%s -- the mosaic index is empty. Harvest "
                    "at least one granule, then call publish_coverage().",
                    workspace,
                    coverage_name,
                )

        if verify and published:
            missing = self.verify_granules(
                definition, harvested, uploaded=[str(p) for p in definition.upload_files]
            )
            if missing:
                lost = {location for location, _ in missing}
                harvested = [h for h in harvested if h not in lost]
                failed += missing
        if reused_index and published and not index_cleared:
            # The index may still hold rows from an earlier store.  Only rows
            # for granules known to have landed are kept, and only when the
            # check above ran: pruning against unverified harvests could
            # empty the index.
            if verify:
                self._prune_index(definition, harvested)
            else:
                log.warning(
                    "Index of %s:%s was reused from an earlier store and keeps "
                    "its existing rows (verify=False, so what landed is unknown).",
                    workspace, coverage_name,
                )

        return MosaicResult(
            workspace=workspace,
            store=store,
            coverage=coverage_name,
            published=published,
            created=not existed,
            harvested=harvested,
            failed=failed,
        )

    @staticmethod
    def _orphaned_index_suspected(definition: MosaicDefinition, exc: GeoServerHTTPError) -> bool:
        """Whether a publish failure looks like the orphaned-table symptom.

        A store created over a PostGIS table left behind by an earlier store
        of the same name accepts every harvest, indexes nothing, and cannot be
        published: the reader reports no coverage.  Only a definition that
        owns a database index can be in that state.
        """
        if not isinstance(definition.index, (PostgisIndex, ExistingIndexStore)):
            return False
        if definition.is_external or definition.resolved_indexer().use_existing_schema:
            return False
        return exc.status_code == 500 and "coveragename is unavailable" in exc.body.lower()

    def _rebuild_over_orphaned_index(
        self, definition: MosaicDefinition, exc: GeoServerHTTPError
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Bootstrap a mosaic configuration from the leftover table, then build.

        Runs once, only for a store this call created.  A store created with
        ``UseExistingSchema=true`` over the table initialises from its rows
        and can be published, but harvesting into it inserts nothing -- it is
        useful only for the configuration file it writes.  With that file in
        place the store is deleted and created again from the real
        definition, over the kept directory, which does harvest.
        """
        workspace, store = definition.workspace, definition.store
        log.warning(
            "Publishing %s:%s failed (%s). This is what a store built over an "
            "index table left behind by an earlier store looks like; "
            "bootstrapping a configuration from that table and building again.",
            workspace, store, exc.message or exc,
        )
        self._delete_store(workspace, store, None, remove_directory=True)
        indexer = dataclass_replace(definition.resolved_indexer(), use_existing_schema=True)
        adopted = dataclass_replace(definition, indexer=indexer, granules=(), upload_files=())
        self._create_store(adopted)
        probe = next(iter(definition.granules), None)
        if probe is not None:
            # A harvest is what makes the reader write its configuration.
            self.client.harvest(workspace, store, probe)
        try:
            self.publish_coverage(adopted)
        except GeoServerHTTPError:
            log.error(
                "Bootstrapping %s:%s over the leftover index table failed too. "
                "An empty leftover table cannot be recovered over REST: drop "
                "table %r in the database and run again.",
                workspace, store, definition.resolved_indexer().name,
            )
            raise
        self.client.delete_coverage_store(workspace, store, recurse=True)
        self._create_store(definition)
        if definition.upload_files:
            self.add_files(workspace, store, definition.upload_files)
        harvested, failed = self.add_granules(workspace, store, definition.granules)
        self.publish_coverage(definition)
        return harvested, failed

    def _empty_indexes(self, workspace: str, store: str) -> bool:
        """Drop every granule from each published coverage's index.

        Returns True when there was at least one coverage and every index was
        emptied.  A store created but never published exposes no index over
        REST; that is not an error, just False.  ``purge`` is deliberately
        not set -- this removes index entries, never granule files.
        """
        try:
            coverages = self.client.list_coverages(workspace, store)
        except GeoServerError as exc:
            log.warning("Could not list coverages of %s:%s (%s)", workspace, store, exc)
            return False
        cleared = bool(coverages)
        for coverage in coverages:
            try:
                self.client.delete_granules(workspace, store, coverage, purge=False)
                log.debug("Emptied granule index for %s:%s", workspace, coverage)
            except GeoServerError as exc:
                cleared = False
                log.warning(
                    "Could not empty the granule index for %s:%s (%s); stale "
                    "rows will be pruned after the rebuild where possible.",
                    workspace, coverage, exc,
                )
        return cleared

    def _prune_index(self, definition: MosaicDefinition, harvested: list[str]) -> None:
        """Remove rows a reused table holds for granules outside ``harvested``.

        ``harvested`` must be the verified list.  Pruning happens only when
        the definition is exact enough to say what belongs: every harvested
        location names one granule, nothing was uploaded, and at least one
        granule landed (an index emptied outright leaves the mosaic unable to
        describe itself).  Otherwise the existing rows are kept and the log
        says so.
        """
        workspace, store = definition.workspace, definition.store
        coverage = definition.resolved_coverage()
        exact = all(_names_one_granule(l) for l in harvested)
        if not harvested or not exact or definition.upload_files:
            log.warning(
                "Index of %s:%s was reused from an earlier store and keeps its "
                "existing rows; the definition's granules cannot be told apart "
                "from them (directory harvest or uploaded files). Use "
                "remove_granules() to drop what should not be there.",
                workspace, coverage,
            )
            return
        keep = {_index_location(l) for l in harvested}
        stale = [
            str(g.get("properties", {}).get("location"))
            for g in self.client.iter_granules(workspace, store, coverage)
            if str(g.get("properties", {}).get("location")) not in keep
        ]
        for start in range(0, len(stale), 50):
            batch = stale[start : start + 50]
            cql = "location IN (" + ",".join(_cql_string(l) for l in batch) + ")"
            self.client.delete_granules(workspace, store, coverage, filter=cql, purge=False)
        if stale:
            log.info("Pruned %d stale granule(s) from reused index %s:%s", len(stale), workspace, coverage)

    def _create_store(self, definition: MosaicDefinition) -> bool:
        """Create the store; True when it was built over a kept directory.

        A directory with a mosaic configuration in it is reused (and the
        configuration patched to this definition); one without is junk from
        an aborted earlier attempt and is removed so the store starts clean.
        """
        workspace, store = definition.workspace, definition.store
        if definition.location is not None:
            self.client.create_store_from_external_dir(
                workspace, store, definition.location, configure="none"
            )
            return False
        reused = _keeps_directory(definition) and self._patch_mosaic_config(definition)
        if not reused:
            self._remove_store_directory(workspace, store, orphaned=True)
        archive = build_config_archive(definition)
        self.client.upload_mosaic_archive(workspace, store, archive, configure="none")
        return reused

    def _mosaic_config_path(self, definition: MosaicDefinition) -> str:
        """Where GeoServer keeps the configuration it derived for this mosaic."""
        directory = self.client.store_directory(definition.workspace, definition.store)
        return f"{directory}/{definition.resolved_indexer().name}.properties"

    def _patch_mosaic_config(self, definition: MosaicDefinition) -> bool:
        """Overlay this definition's settings onto GeoServer's mosaic config.

        Returns False when there is no such file (nothing to reuse).  Keys
        this definition does not set are left as GeoServer wrote them: the
        envelope, resolution levels and the like are derived from the
        granules and stay valid.
        """
        path = self._mosaic_config_path(definition)
        try:
            if not self.client.resource_exists(path):
                return False
            current = properties.loads(self.client.read_resource(path))
        except GeoServerError as exc:
            log.warning("Could not read %s (%s); starting the store from scratch", path, exc)
            return False
        wanted = definition.resolved_indexer().to_properties()
        if definition.cog is not None:
            wanted.update(definition.cog.to_properties())
        current.update({key: value for key, value in wanted.items() if value is not None})
        self.client.write_resource(
            path, properties.dumps(current, header="Patched by geoserver-rest-mosaic")
        )
        log.info("Reusing %s with this definition's settings patched in", path)
        return True

    def _delete_store(
        self,
        workspace: str,
        store: str,
        purge: str | bool | None,
        *,
        remove_directory: bool,
    ) -> None:
        """Delete a store, and the directory it leaves behind if it had one.

        ``purge`` is sent only when given; the default omits the parameter
        entirely.  A pre-provisioned store lives outside the data directory,
        so there is nothing of it under ``data/`` to remove.
        """
        try:
            self.client.delete_coverage_store(workspace, store, recurse=True, purge=purge)
        except GeoServerHTTPError as exc:
            # An explicit purge on a PostGIS index makes GeoServer try to drop
            # the database; the usual outcome is this 500 with the store gone.
            if self.client.coverage_store_exists(workspace, store):
                raise
            log.warning(
                "GeoServer reported %s while purging %s:%s, but the store is "
                "gone; continuing", exc.message or exc, workspace, store,
            )
        if remove_directory:
            self._remove_store_directory(workspace, store, orphaned=False)

    def _remove_store_directory(self, workspace: str, store: str, *, orphaned: bool) -> None:
        """Delete ``data/<workspace>/<store>`` from the data directory.

        Best-effort: the resource API may be disabled or the directory may
        not exist.  ``orphaned`` only changes the log message -- a directory
        found without a store is a leftover from an earlier delete.
        """
        path = self.client.store_directory(workspace, store)
        try:
            if not self.client.resource_exists(path):
                return
            if orphaned:
                log.warning(
                    "Removing %s, left behind by an earlier store of the same "
                    "name; the mosaic configuration in it would otherwise "
                    "override the one being uploaded", path,
                )
            self.client.delete_resource(path)
        except GeoServerError as exc:
            log.warning(
                "Could not remove %s (%s). If the store fails to initialise or "
                "ignores its new configuration, remove that directory on the "
                "GeoServer host.", path, exc,
            )

    def _can_publish(
        self, definition: MosaicDefinition, harvested: list[str], existed: bool
    ) -> bool:
        """Whether the index holds enough for GeoServer to describe a coverage."""
        if harvested or definition.upload_files:
            return True
        if definition.is_external:
            # GeoServer walked the directory when it registered the store; an
            # empty directory would have failed that step already.
            return True
        # A store built over a pre-existing index table starts non-empty.
        if definition.resolved_indexer().use_existing_schema:
            return True
        # An already-published coverage was describable once; refresh it.
        return existed and self.client.coverage_exists(
            definition.workspace, definition.store, definition.resolved_coverage()
        )

    def publish_coverage(self, definition: MosaicDefinition) -> None:
        """Create or update the coverage that exposes the mosaic as a layer."""
        workspace, store = definition.workspace, definition.store
        coverage = definition.resolved_coverage()
        body = payloads.coverage(
            coverage,
            # The native name is how GeoServer finds the mosaic inside the
            # store; it must equal the indexer's Name.
            native_name=self._native_name(definition),
            title=definition.title or coverage,
            abstract=definition.abstract,
            srs=definition.srs,
            keywords=list(definition.keywords) or None,
            dimensions=definition.resolved_dimensions(),
            parameters={**DEFAULT_COVERAGE_PARAMETERS, **definition.parameters},
        )
        if self.client.coverage_exists(workspace, store, coverage):
            self.client.update_coverage(workspace, store, coverage, body)
            log.info("Updated coverage %s:%s", workspace, coverage)
        else:
            self.client.create_coverage(workspace, store, body)
            log.info("Published coverage %s:%s", workspace, coverage)

        if definition.default_style:
            self.client.set_default_style(workspace, coverage, definition.default_style)

    def _native_name(self, definition: MosaicDefinition) -> str:
        """The coverage name GeoServer's reader reports for this mosaic.

        For a store configured here it is the indexer ``Name``.  For a
        pre-provisioned directory it is whatever that directory's indexer
        says, so it is asked of the server unless ``mosaic_name`` pins it.
        """
        if not definition.is_external:
            return definition.resolved_indexer().name
        if definition.mosaic_name:
            return definition.mosaic_name
        workspace, store = definition.workspace, definition.store
        names = self.client.list_native_coverages(workspace, store)
        if len(names) == 1:
            return names[0]
        raise MosaicConfigurationError(
            f"Store {workspace}:{store} at {definition.location!r} exposes "
            f"{names or 'no'} coverages; set mosaic_name to pick one"
            if names
            else f"Store {workspace}:{store} at {definition.location!r} exposes "
            "no coverages; check that GeoServer can read the directory and "
            "that it holds granules"
        )

    # -- deletion ----------------------------------------------------------

    def delete(
        self,
        workspace: str,
        store: str,
        *,
        purge: str | bool | None = None,
        remove_directory: bool = False,
    ) -> bool:
        """Delete a mosaic store so that its name can be built again.

        The granule index is emptied first (row by row, never with
        ``purge``: on a PostGIS index that makes GeoServer try to drop the
        whole database, see :meth:`create`).  The store's directory under
        the data directory is kept, on purpose: the mosaic configuration in
        it is what lets a later store of the same name work over the index
        table that also survives.  ``remove_directory=True`` deletes it
        anyway, for a store that will not come back; do that for a
        PostGIS-indexed mosaic only if you also drop its table.  A store
        rooted outside the data directory (a pre-provisioned mosaic) has
        nothing here to remove.  Returns False when there was no such store.
        """
        if not self.client.coverage_store_exists(workspace, store):
            return False
        owned = self._store_is_in_data_dir(workspace, store)
        if owned:
            self._empty_indexes(workspace, store)
        self._delete_store(
            workspace, store, purge, remove_directory=owned and remove_directory
        )
        log.info("Deleted store %s:%s", workspace, store)
        return True

    def delete_workspace(
        self,
        workspace: str,
        *,
        purge: str | bool | None = None,
        remove_directories: bool = False,
    ) -> bool:
        """Delete a workspace and every coverage store in it, cleanly.

        ``client.delete_workspace(recurse=True)`` drops the catalog entries
        only and leaves every index full of rows.  Each store goes through
        :meth:`delete` first, which empties its index; the directories are
        kept unless ``remove_directories`` is set.  Returns False when there
        was no such workspace.
        """
        if not self.client.workspace_exists(workspace):
            return False
        for store in self.client.list_coverage_stores(workspace):
            self.delete(workspace, store, purge=purge, remove_directory=remove_directories)
        self.client.delete_workspace(workspace, recurse=True)
        log.info("Deleted workspace %s", workspace)
        return True

    def _store_is_in_data_dir(self, workspace: str, store: str) -> bool:
        """Whether GeoServer keeps this store under ``data/<workspace>/<store>``.

        A store created from an upload reports a relative ``file:data/...``
        URL; one registered on an external directory reports where that is.
        """
        url = str(self.client.get_coverage_store(workspace, store).get("url") or "")
        expected = self.client.store_directory(workspace, store)
        path = host_path(url).lstrip("/")
        return path.rstrip("/") == expected or path.rstrip("/").endswith("/" + expected)

    # -- granules ----------------------------------------------------------

    def add_granules(
        self,
        workspace: str,
        store: str,
        locations: Iterable[str],
        *,
        stop_on_error: bool = False,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Harvest granules one at a time.

        Returns ``(harvested, failures)`` where each failure is a
        ``(location, message)`` pair.  Harvesting continues past a bad granule
        by default: in a bulk load, one unreadable file should not cost you the
        other several hundred.
        """
        harvested: list[str] = []
        failures: list[tuple[str, str]] = []
        for location in locations:
            try:
                self.client.harvest(workspace, store, location)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                log.error("Failed to harvest %s: %s", location, exc)
                failures.append((location, str(exc)))
                if stop_on_error:
                    raise
            else:
                harvested.append(location)
        return harvested, failures

    def add_files(self, workspace: str, store: str, files: Iterable[Path]) -> list[str]:
        """Upload local granule files into an existing mosaic.

        The files are zipped and PUT to ``file.imagemosaic``, which on an
        existing store means "harvest these".  Returns the uploaded filenames.
        Use :meth:`add_granules` for files GeoServer can already reach.
        """
        files = [Path(p) for p in files]
        if not files:
            return []
        _check_files_exist(files)
        self.client.upload_mosaic_archive(workspace, store, build_granule_archive(files))
        names = [p.name for p in files]
        log.info("Uploaded %d granule file(s) into %s:%s", len(names), workspace, store)
        return names

    def verify_granules(
        self,
        definition: MosaicDefinition,
        locations: Iterable[str],
        *,
        uploaded: Iterable[str] = (),
    ) -> list[tuple[str, str]]:
        """Check which harvested locations actually reached the index.

        The harvest endpoints answer ``202`` with no body for any input, so
        the index is the only evidence.  Locations that name a single granule
        (remote URLs, file paths with an extension) are looked up by name, in
        batches, with a CQL ``location IN (...)`` filter, so the cost scales
        with what was harvested rather than with the index.  ``uploaded``
        files live in the store directory under a server-side path this
        client does not know, so they are matched on their basename with
        ``location LIKE '%/<name>'``.  A directory harvest cannot be matched
        at all and is only checked for having produced a non-empty index.

        Requires the coverage to be published, since the index is reached
        through it.  Returns ``(location, message)`` pairs for the missing.
        """
        locations = list(locations)
        uploaded = list(uploaded)
        if not locations and not uploaded:
            return []
        workspace, store = definition.workspace, definition.store
        coverage = definition.resolved_coverage()
        # Only an AbsolutePath index stores locations as given; a relative one
        # would make every exact lookup a false failure.
        exact_ok = definition.is_external is False and definition.resolved_indexer().absolute_path
        exact = [l for l in locations if exact_ok and _names_one_granule(l)]
        vague = [l for l in locations if l not in exact]

        failures: list[tuple[str, str]] = []
        for start in range(0, len(exact), 100):
            batch = exact[start : start + 100]
            wanted = {_index_location(l): l for l in batch}
            cql = "location IN (" + ",".join(_cql_string(k) for k in wanted) + ")"
            found = {
                str(g.get("properties", {}).get("location"))
                for g in self.client.list_granules(
                    workspace, store, coverage, filter=cql, limit=len(batch)
                )
            }
            failures += [
                (original, _NOT_INDEXED.format(what="absent from the index"))
                for key, original in wanted.items()
                if key not in found
            ]
        for start in range(0, len(uploaded), 50):
            batch = uploaded[start : start + 50]
            by_name = {Path(p).name: p for p in batch}
            cql = " OR ".join(f"location LIKE {_cql_string('%/' + name)}" for name in by_name)
            found = {
                Path(str(g.get("properties", {}).get("location"))).name
                for g in self.client.list_granules(
                    workspace, store, coverage, filter=cql, limit=len(batch)
                )
            }
            failures += [
                (original, _NOT_INDEXED.format(what="absent from the index"))
                for name, original in by_name.items()
                if name not in found
            ]
        if vague and not self.client.list_granules(workspace, store, coverage, limit=1):
            failures += [(l, _NOT_INDEXED.format(what="the index is empty")) for l in vague]
        return failures

    def granule_count(self, definition: MosaicDefinition) -> int:
        """Number of granules currently in the index."""
        return sum(
            1
            for _ in self.client.iter_granules(
                definition.workspace, definition.store, definition.resolved_coverage()
            )
        )

    def remove_granules(
        self, definition: MosaicDefinition, *, filter: str, purge: bool = False
    ) -> None:
        """Drop granules matching a CQL ``filter`` from the index.

        ``filter`` is mandatory here even though the REST API allows omitting
        it, because "delete every granule" is rarely what a caller means and is
        not recoverable.
        """
        if not filter:
            raise MosaicConfigurationError(
                "A CQL filter is required; to empty the index deliberately, "
                "call client.delete_granules() directly."
            )
        self.client.delete_granules(
            definition.workspace,
            definition.store,
            definition.resolved_coverage(),
            filter=filter,
            purge=purge,
        )

    # -- inspection --------------------------------------------------------

    def describe(self, definition: MosaicDefinition) -> dict[str, Any]:
        """Summarise the live state of the mosaic, for diagnostics."""
        workspace = definition.workspace
        store = definition.store
        coverage = definition.resolved_coverage()
        info: dict[str, Any] = {
            "workspace": workspace,
            "store": store,
            "coverage": coverage,
            "store_exists": self.client.coverage_store_exists(workspace, store),
            "coverage_exists": False,
            "index_schema": None,
            "granule_count": None,
        }
        if not info["store_exists"]:
            return info
        info["coverage_exists"] = self.client.coverage_exists(workspace, store, coverage)
        if info["coverage_exists"]:
            try:
                info["index_schema"] = self.client.index_schema(workspace, store, coverage)
                info["granule_count"] = self.granule_count(definition)
            except NotFoundError:
                # A published coverage whose index is gone: worth surfacing as
                # "unknown" rather than crashing a diagnostic call.
                pass
        return info


_NOT_INDEXED = (
    "accepted by GeoServer (202) but {what}; check the server log for the "
    "reader error"
)


def _keeps_directory(definition: MosaicDefinition) -> bool:
    """Whether a store of this definition reuses its data-directory folder.

    Only a table-backed index needs the bridge the folder provides; a
    shapefile index lives in the folder itself and is rebuilt from scratch.
    """
    return not definition.is_external and isinstance(
        definition.index, (PostgisIndex, ExistingIndexStore)
    )


def _names_one_granule(location: str) -> bool:
    """A location that addresses one file, as opposed to a directory to scan."""
    if is_remote_location(location):
        return True
    return bool(Path(host_path(location)).suffix)


def _index_location(location: str) -> str:
    """The form a harvested location takes in the index's ``location`` column."""
    if is_remote_location(location):
        return location
    return host_path(location)


def _cql_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"

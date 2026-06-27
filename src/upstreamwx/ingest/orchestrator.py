"""Ingestion orchestrator (PRD §11.2 component 4).

Assembles a normalized :class:`IngestBundle` for a mission from every source —
NWS products/AFD, Open-Meteo derived fields, the SREF processor over the upstream
domain, and the SPC outlook — with graceful degradation when a non-mandatory
source is down (NFR-6). The NWS AFD is mandatory (FR-5); its failure is surfaced.

This is the glue from a mission to engine-ready inputs:

    mission -> watershed delineation -> ingest bundle -> HazardInputs -> engine.assess
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

from shapely.geometry.base import BaseGeometry

from ..engine.models import HazardInputs, Mission
from ..watershed import clip_watershed, delineate_cached
from . import href_provider, nws, openmeteo, spc, sref_provider
from .base import IngestBundle, to_hazard_inputs

# Providers that don't need the watershed polygon; (name, module).
_POINT_PROVIDERS = (nws, openmeteo, spc)
MANDATORY = {"nws"}

# Bundle fields that accumulate from more than one source and must be *combined* on merge
# rather than copied: provenance dicts/lists every group contributes to.
_MERGE_DICT_FIELDS = frozenset({"sources_ok", "member_support"})
_MERGE_LIST_FIELDS = frozenset({"notes"})


def _run_point_provider(provider, mission: Mission) -> IngestBundle:
    """Run one point provider into its own bundle (NFR-6 degradation contained per source).

    Each concurrent task owns a private bundle so the providers — which mutate the bundle in
    place — never race on shared state; results are merged deterministically by the caller.
    """
    bundle = IngestBundle()
    try:
        provider.fetch(mission, bundle)
    except Exception as exc:  # noqa: BLE001 — degrade per NFR-6
        bundle.sources_ok[provider.NAME] = False
        bundle.notes.append(f"{provider.NAME}: unavailable ({type(exc).__name__}).")
    return bundle


def _run_watershed_and_ensembles(
    mission: Mission, polygon: BaseGeometry | None, cycle
) -> IngestBundle:
    """Delineate (+ RoC clip) the domain, then aggregate SREF and HREF over it (FR-3, FR-7/7a).

    Runs as one task into its own bundle. SREF and HREF stay sequential here (HREF reads the
    SREF signal for the cross-ensemble agreement, FR-17, and this keeps cfgrib decoding off
    concurrent threads); the whole chain runs concurrently with the point providers above.
    """
    bundle = IngestBundle()

    # SREF over the upstream domain (needs the watershed polygon). Delineate
    # pour-point-exact (NLDI raindrop two-step) with the WBD HUC-12 trace as the
    # snap-free fallback; cache on disk so repeat missions reuse it.
    if polygon is None:
        try:
            basin = delineate_cached(mission.lat, mission.lon)
            bundle.upstream = basin
            polygon = basin.polygon
            bundle.sources_ok["watershed"] = True
            domain = basin.flowline_name or (
                f"comid {basin.comid}" if basin.comid else "pour point"
            )
            bundle.notes.append(
                f"watershed: {basin.area_km2:.0f} km² to {domain} via {basin.method}."
            )
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok["watershed"] = False
            bundle.notes.append(f"watershed: delineation failed ({type(exc).__name__}).")

        # Radius of Concern (FR-3): clip the delineated basin to the user's disk so the
        # SREF/HREF aggregation runs over the bounded domain. Defensive — a clip failure
        # must never crash the briefing (NFR-6); fall back to the full watershed.
        if polygon is not None and mission.radius_km:
            try:
                clip = clip_watershed(polygon, mission.lat, mission.lon, mission.radius_km)
                polygon = clip.kept
                bundle.roc_radius_km = mission.radius_km
                bundle.roc_disk = clip.disk
                bundle.roc_excluded = clip.excluded
                bundle.roc_kept_area_km2 = clip.kept_area_km2
                bundle.notes.append(
                    f"radius of concern: clipped to {clip.kept_area_km2:.0f} km² "
                    f"within {mission.radius_km:.0f} km of origin."
                )
            except Exception as exc:  # noqa: BLE001
                bundle.notes.append(
                    f"radius of concern: clip failed ({type(exc).__name__}); full watershed used."
                )

    bundle.aggregation_polygon = polygon
    if polygon is not None:
        try:
            sref_provider.fetch(mission, bundle, polygon, cycle=cycle)
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok[sref_provider.NAME] = False
            bundle.notes.append(f"sref: unavailable ({type(exc).__name__}).")

        # HREF same-day supplement (FR-7a): conditional, runs after SREF so it can
        # record SREF<->HREF agreement; degrades gracefully like any non-mandatory source.
        try:
            href_provider.fetch(mission, bundle, polygon)
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok[href_provider.NAME] = False
            bundle.notes.append(f"href: unavailable ({type(exc).__name__}).")

    return bundle


def _merge_into(dest: IngestBundle, src: IngestBundle) -> None:
    """Fold a task's bundle into ``dest`` in a fixed, timing-independent order (NFR-4).

    Provenance dicts/lists accumulate (every group contributes); every other field has exactly
    one owning source, so we copy a value only when the task actually set it (differs from a
    fresh default). Because each field has a single owner and tasks merge in a fixed order, the
    merged bundle — and therefore the engine inputs — are identical regardless of which task
    finished first.
    """
    for f in fields(IngestBundle):
        name = f.name
        if name in _MERGE_DICT_FIELDS:
            getattr(dest, name).update(getattr(src, name))
        elif name in _MERGE_LIST_FIELDS:
            getattr(dest, name).extend(getattr(src, name))
        else:
            value = getattr(src, name)
            if value != getattr(_DEFAULT_BUNDLE, name):
                setattr(dest, name, value)


# A pristine bundle whose field values are the "unset" baseline used by ``_merge_into``.
_DEFAULT_BUNDLE = IngestBundle()


def gather(
    mission: Mission,
    *,
    polygon: BaseGeometry | None = None,
    cycle=None,
) -> IngestBundle:
    """Gather all sources into a bundle; degrade gracefully on non-mandatory failures.

    The point providers (NWS, Open-Meteo, SPC) and the watershed→SREF/HREF chain are mutually
    independent, so they run concurrently — each on a private bundle — collapsing the briefing's
    serial network latency to roughly the slowest single branch instead of their sum. Results
    are merged deterministically (NFR-4); only I/O is parallelised, never the engine.
    """
    bundle = IngestBundle()

    with ThreadPoolExecutor(max_workers=len(_POINT_PROVIDERS) + 1) as executor:
        point_futures = [
            executor.submit(_run_point_provider, provider, mission)
            for provider in _POINT_PROVIDERS
        ]
        ensemble_future = executor.submit(
            _run_watershed_and_ensembles, mission, polygon, cycle
        )
        # Merge in a fixed order (point providers, then the ensemble branch) so notes and
        # source order do not depend on completion timing.
        for future in point_futures:
            _merge_into(bundle, future.result())
        _merge_into(bundle, ensemble_future.result())

    failed_mandatory = [s for s in MANDATORY if bundle.sources_ok.get(s) is False]
    if failed_mandatory:
        bundle.notes.append(f"WARNING: mandatory source(s) unavailable: {failed_mandatory}.")
    return bundle


def gather_inputs(
    mission: Mission,
    *,
    polygon: BaseGeometry | None = None,
    cycle=None,
) -> tuple[HazardInputs, IngestBundle]:
    """Convenience: gather a bundle and map it to engine HazardInputs."""
    # Conservative default: assume a wet party (FR-16). A dry cave with no
    # immersion is a per-mission override the caller passes explicitly later.
    bundle = gather(mission, polygon=polygon, cycle=cycle)
    return to_hazard_inputs(bundle, dry_party=False), bundle

#!/usr/bin/env python3
"""Offline smoke checks for the Arkansas GIS Office 0.5 m DEM adapter.

No network access: discovery is exercised only on its offline guards, and
ranking uses hand-built candidates.

    python my-farm-advisor/data-pipeline/src/scripts/tests/smoke_arkansas_dem_adapter.py
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[5]
sys.path.insert(0, str(REPO_ROOT / "my-farm-advisor" / "terrain" / "dem-terrain" / "src"))

from dem_terrain.arkansas_agio import (  # noqa: E402
    DEFAULT_SERVICE_URL,
    ArkansasAGIOAdapter,
    build_export_urls,
    cache_name_for_url,
)
from dem_terrain.source_resolver import (  # noqa: E402
    ADAPTER_ARKANSAS_AGIO,
    ADAPTER_USGS_TNM,
    REGION_POLICY_REGIONAL,
    REGION_POLICY_US,
    SURFACE_DEM,
    SourceAOI,
    SourceCandidate,
    instantiate_default_adapters,
    select_best_candidate,
)


class SmokeFailure(AssertionError):
    """Raised when a smoke check fails."""


def _candidate(adapter_id: str, resolution: float, region_policy: str) -> SourceCandidate:
    return SourceCandidate(
        adapter_id=adapter_id,
        adapter_name=adapter_id,
        source_name=adapter_id,
        source_urls=(f"https://example.invalid/{adapter_id}.tif",),
        region_policy=region_policy,
        country_hints=("US",),
        resolution_m=resolution,
        surface_type=SURFACE_DEM,
        coverage_score=1.0,
        direct_no_auth=True,
    )


def main() -> int:
    inside = SourceAOI(country="US", bbox_wgs84=(-90.853, 35.656, -90.848, 35.666))
    outside = SourceAOI(country="US", bbox_wgs84=(-88.80, 41.90, -88.79, 41.91))
    if ArkansasAGIOAdapter(network_enabled=True).discover(outside) != ():
        raise SmokeFailure("adapter must ignore AOIs outside Arkansas")
    if ArkansasAGIOAdapter(network_enabled=False).discover(inside) != ():
        raise SmokeFailure("adapter must not touch the network when disabled")
    if not any(a.adapter_id == ADAPTER_ARKANSAS_AGIO for a in instantiate_default_adapters()):
        raise SmokeFailure("adapter missing from default adapter registry")
    print("PASS: offline guards and registry")

    bounds = (694301.3, 3947000.2, 694883.1, 3949600.0)  # ~2.6 km tall -> 2 strips at 0.5 m
    urls = build_export_urls(DEFAULT_SERVICE_URL, bounds)
    if urls != build_export_urls(DEFAULT_SERVICE_URL, bounds):
        raise SmokeFailure("export URLs are not deterministic")
    if len(urls) != 2:
        raise SmokeFailure(f"expected 2 strips, got {len(urls)}")
    first = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(urls[0]).query))
    x0, _, x1, top = (float(v) for v in first["bbox"].split(","))
    cols, rows = (int(v) for v in first["size"].split(","))
    if abs((x1 - x0) / cols - 0.5) > 1e-9 or rows != 4000 or x0 % 0.5 or top % 0.5:
        raise SmokeFailure(f"strip not snapped to the 0.5 m grid: {first['bbox']} {first['size']}")
    if first["pixelType"] != "F32" or first["imageSR"] != "26915":
        raise SmokeFailure("export must be raw float32 in EPSG:26915")
    if len({cache_name_for_url(u) for u in urls}) != 2:
        raise SmokeFailure("cache names must be unique per strip")
    print("PASS: deterministic, grid-snapped strip export URLs")

    arkansas = _candidate(ADAPTER_ARKANSAS_AGIO, 0.5, REGION_POLICY_REGIONAL)
    usgs = _candidate(ADAPTER_USGS_TNM, 1.0, REGION_POLICY_US)
    if select_best_candidate((usgs, arkansas), aoi=inside).selected.adapter_id != ADAPTER_ARKANSAS_AGIO:
        raise SmokeFailure("0.5 m Arkansas DEM must outrank USGS 1 m")
    if select_best_candidate((usgs,), aoi=inside).selected.adapter_id != ADAPTER_USGS_TNM:
        raise SmokeFailure("USGS must remain the fallback")
    print("PASS: Arkansas 0.5 m ranks ahead of USGS 1 m; USGS remains fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())

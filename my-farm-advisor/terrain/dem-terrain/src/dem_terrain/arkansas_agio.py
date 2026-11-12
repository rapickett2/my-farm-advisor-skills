"""Arkansas GIS Office (AGIO) 0.5 m statewide lidar DEM adapter.

Discovers and downloads clips of the free public ``DEM_HALF_METER_2026``
ArcGIS ImageServer (gis.arkansas.gov) for AOIs inside Arkansas. Each candidate
carries one deterministic ``exportImage`` URL per strip (raw float32 meters,
native EPSG:26915, snapped to the 0.5 m grid, AOI plus a margin), so the source
reference records exactly what was requested and cache files are keyed by URL.

Import-safe: importing reads no environment, creates no directories, and does
no network I/O. ``pyproj`` is imported lazily during discovery only.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .source_resolver import (
    ADAPTER_ARKANSAS_AGIO,
    REGION_POLICY_REGIONAL,
    SURFACE_DEM,
    SourceAdapter,
    SourceAOI,
    SourceCandidate,
)

DEFAULT_SERVICE_URL = (
    "https://gis.arkansas.gov/arcgis/rest/services/ImageServices/DEM_HALF_METER_2026/ImageServer"
)
SERVICE_WKID = 26915
PIXEL_SIZE_M = 0.5
NODATA = -9999.0
MAX_ROWS = 4000
MAX_COLS = 14000
DEFAULT_MARGIN_M = 60.0
ARKANSAS_BBOX_WGS84 = (-94.62, 33.00, -89.64, 36.50)
USER_AGENT = "my-farm-advisor-dem-terrain/arkansas-agio"
CITATION = (
    "Arkansas GIS Office, DEM_HALF_METER_2026 (0.5 m statewide lidar DEM), "
    "ArcGIS ImageServer at gis.arkansas.gov"
)
LICENSE = "Arkansas GIS Office public image service; free download; no license text published"
VERTICAL_DATUM_WARNING = (
    "vertical_datum_unpublished: assumed NAVD88 meters (agrees with the AGIO NAVD88 1 m layer within ~0.1-0.2 m)"
)


def _intersects(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
    return first[0] <= second[2] and first[2] >= second[0] and first[1] <= second[3] and first[3] >= second[1]


def _snap(bounds: tuple[float, float, float, float], pixel: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    return (
        math.floor(x0 / pixel) * pixel,
        math.floor(y0 / pixel) * pixel,
        math.ceil(x1 / pixel) * pixel,
        math.ceil(y1 / pixel) * pixel,
    )


def build_export_urls(
    service_url: str,
    bounds_utm: tuple[float, float, float, float],
    *,
    pixel: float = PIXEL_SIZE_M,
    wkid: int = SERVICE_WKID,
) -> tuple[str, ...]:
    """Return deterministic exportImage URLs covering ``bounds_utm`` in strips."""

    x0, y0, x1, y1 = _snap(bounds_utm, pixel)
    cols = int(round((x1 - x0) / pixel))
    if cols > MAX_COLS:
        raise ValueError(f"AOI too wide for one AGIO export at {pixel} m ({cols} columns)")
    urls: list[str] = []
    top = y1
    while top > y0 + 1e-6:
        bottom = max(y0, top - MAX_ROWS * pixel)
        rows = int(round((top - bottom) / pixel))
        params = {
            "bbox": f"{x0:.1f},{bottom:.1f},{x1:.1f},{top:.1f}",
            "bboxSR": wkid,
            "imageSR": wkid,
            "size": f"{cols},{rows}",
            "format": "tiff",
            "pixelType": "F32",
            "noData": NODATA,
            "noDataInterpretation": "esriNoDataMatchAny",
            "interpolation": "RSP_NearestNeighbor",
            "f": "image",
        }
        urls.append(f"{service_url}/exportImage?{urllib.parse.urlencode(params)}")
        top = bottom
    return tuple(urls)


def cache_name_for_url(url: str) -> str:
    """Stable cache file name for one export URL."""

    return f"agio_dem_0p5m_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}.tif"


class ArkansasAGIOAdapter(SourceAdapter):
    """Arkansas GIS Office 0.5 m lidar DEM via public ArcGIS ImageServer."""

    adapter_id = ADAPTER_ARKANSAS_AGIO
    adapter_name = "Arkansas GIS Office 0.5 m lidar DEM"

    def __init__(
        self,
        *,
        service_url: str = DEFAULT_SERVICE_URL,
        network_enabled: bool = False,
        margin_m: float = DEFAULT_MARGIN_M,
        timeout_seconds: float = 120.0,
        retries: int = 2,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.network_enabled = network_enabled
        self.margin_m = max(0.0, margin_m)
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.last_errors: tuple[dict[str, Any], ...] = ()

    def discover(self, aoi: SourceAOI) -> tuple[SourceCandidate, ...]:
        """Return one candidate when the AOI is inside Arkansas and the service has rasters there."""

        self.last_errors = ()
        if not aoi.bbox_wgs84 or not _intersects(aoi.bbox_wgs84, ARKANSAS_BBOX_WGS84):
            return ()
        if not self.network_enabled:
            return ()
        from pyproj import Transformer  # lazy: keep module import stdlib-only

        transformer = Transformer.from_crs(4326, SERVICE_WKID, always_xy=True)
        minx, miny, maxx, maxy = aoi.bbox_wgs84
        xs, ys = zip(*(transformer.transform(x, y) for x in (minx, maxx) for y in (miny, maxy)))
        bounds = (
            min(xs) - self.margin_m,
            min(ys) - self.margin_m,
            max(xs) + self.margin_m,
            max(ys) + self.margin_m,
        )
        try:
            items = self._query_items(bounds)
        except Exception as exc:  # structured adapter error, not a traceback
            self.last_errors = ({"adapter_id": self.adapter_id, "reason": f"{exc.__class__.__name__}: {exc}"},)
            return ()
        if not items:
            return ()
        urls = build_export_urls(self.service_url, bounds)
        x0, y0, x1, y1 = _snap(bounds, PIXEL_SIZE_M)
        size_mb = (x1 - x0) / PIXEL_SIZE_M * (y1 - y0) / PIXEL_SIZE_M * 4 / 1e6
        names = ", ".join(sorted(str(item.get("Name")) for item in items))
        return (
            SourceCandidate(
                adapter_id=self.adapter_id,
                adapter_name=self.adapter_name,
                source_name=f"AGIO DEM_HALF_METER_2026 ({names})",
                source_urls=urls,
                metadata_urls=(self.service_url,),
                license=LICENSE,
                citation=CITATION,
                region_policy=REGION_POLICY_REGIONAL,
                country_hints=("US",),
                region_hints=("AR", "Arkansas"),
                resolution_m=PIXEL_SIZE_M,
                surface_type=SURFACE_DEM,
                acquisition_date=None,
                publication_date="2026-05-06",
                coverage_score=1.0,
                direct_no_auth=True,
                requires_auth=False,
                estimated_download_size_mb=round(size_mb, 2),
                warnings=(VERTICAL_DATUM_WARNING,),
                source_id=f"agio-dem-half-meter-2026:{names}",
                bbox_wgs84=aoi.bbox_wgs84,
                access_note=(
                    "Clipped per AOI with exportImage (raw float32, EPSG:26915, 0.5 m grid); "
                    "collections recorded in source_name."
                ),
            ),
        )

    def download(self, candidate: SourceCandidate, cache: str | Path) -> Path:
        """Download the first strip (protocol compatibility); use ``download_all`` for every strip."""

        return self.download_all(candidate, cache)[0]

    def download_all(self, candidate: SourceCandidate, cache: str | Path) -> list[Path]:
        """Download every export strip into ``cache`` (reusing files already present)."""

        cache_dir = Path(cache)
        cache_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for url in candidate.source_urls:
            target = cache_dir / cache_name_for_url(url)
            if not target.exists():
                content = self._get(url)
                if not content.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
                    raise RuntimeError(f"AGIO export did not return a GeoTIFF: {content[:200]!r}")
                tmp = target.with_suffix(".part")
                tmp.write_bytes(content)
                tmp.replace(target)
            paths.append(target)
        return paths

    def _query_items(self, bounds: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        envelope = {
            "xmin": bounds[0],
            "ymin": bounds[1],
            "xmax": bounds[2],
            "ymax": bounds[3],
            "spatialReference": {"wkid": SERVICE_WKID},
        }
        params = {
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "Category=1",
            "outFields": "OBJECTID,Name",
            "returnGeometry": "false",
            "f": "json",
        }
        payload = json.loads(self._get(f"{self.service_url}/query?{urllib.parse.urlencode(params)}"))
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return [feature["attributes"] for feature in payload.get("features", [])]

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except Exception:
                if attempt >= self.retries:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")


__all__ = [
    "ARKANSAS_BBOX_WGS84",
    "ArkansasAGIOAdapter",
    "DEFAULT_SERVICE_URL",
    "build_export_urls",
    "cache_name_for_url",
]

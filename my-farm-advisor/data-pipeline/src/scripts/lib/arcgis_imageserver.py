"""Small helpers for clipping rasters from public ArcGIS ImageServers.

Used by the opt-in state lidar and orthoimagery ingest scripts. Exports are
snapped to the requested pixel grid and split into row strips under the
server's export limits, then mosaicked into one GeoTIFF written atomically.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.merge import merge

USER_AGENT = "my-farm-advisor-data-pipeline/arcgis-imageserver"
MAX_ROWS = 4000
MAX_COLS = 14000


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def request_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    for attempt in range(3):
        try:
            response = session.get(url, params={**(params or {}), "f": "json"}, timeout=90)
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload
        except (requests.RequestException, RuntimeError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def service_info(session: requests.Session, service_url: str) -> dict:
    info = request_json(session, service_url)
    sr = info.get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if not wkid:
        raise RuntimeError(f"service has no spatial reference wkid: {service_url}")
    return {
        "wkid": int(wkid),
        "name": info.get("name") or service_url.rstrip("/").split("/")[-2],
        "band_count": int(info.get("bandCount") or 1),
        "pixel_type": info.get("pixelType"),
        "pixel_size": float(info.get("pixelSizeX") or 1.0),
    }


def snap(bounds: tuple[float, ...], pixel: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    return (
        math.floor(x0 / pixel) * pixel,
        math.floor(y0 / pixel) * pixel,
        math.ceil(x1 / pixel) * pixel,
        math.ceil(y1 / pixel) * pixel,
    )


def _export_strip(session: requests.Session, service_url: str, params: dict) -> bytes:
    for attempt in range(3):
        try:
            response = session.get(f"{service_url}/exportImage", params=params, timeout=180)
            response.raise_for_status()
            if "tiff" not in response.headers.get("content-type", ""):
                raise RuntimeError(response.text[:300])
            return response.content
        except (requests.RequestException, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def export_clip(
    session: requests.Session,
    service_url: str,
    bounds: tuple[float, ...],
    *,
    pixel: float,
    wkid: int,
    target: Path,
    pixel_type: str = "F32",
    nodata: float | None = -9999.0,
    interpolation: str = "RSP_NearestNeighbor",
    mosaic_rule: dict | None = None,
    compress: str = "deflate",
    max_rows: int = MAX_ROWS,
    max_cols: int = MAX_COLS,
) -> list[str]:
    """Export ``bounds`` (in ``wkid``) at ``pixel`` metres to ``target``; return request URLs.

    The area is tiled into blocks of at most ``max_rows`` x ``max_cols`` pixels;
    some servers reject large reprojected requests well below their advertised
    maxImageWidth/maxImageHeight.
    """

    x0, y0, x1, y1 = snap(bounds, pixel)
    requested: list[str] = []
    datasets = []
    memfiles = []
    try:
        top = y1
        while top > y0 + 1e-6:
            bottom = max(y0, top - max_rows * pixel)
            rows = int(round((top - bottom) / pixel))
            left = x0
            while left < x1 - 1e-6:
                right = min(x1, left + max_cols * pixel)
                cols = int(round((right - left) / pixel))
                params = {
                    "bbox": f"{left},{bottom},{right},{top}",
                    "bboxSR": wkid,
                    "imageSR": wkid,
                    "size": f"{cols},{rows}",
                    "format": "tiff",
                    "pixelType": pixel_type,
                    "interpolation": interpolation,
                    "f": "image",
                }
                if nodata is not None:
                    params.update({"noData": nodata, "noDataInterpretation": "esriNoDataMatchAny"})
                if mosaic_rule is not None:
                    params["mosaicRule"] = json.dumps(mosaic_rule)
                requested.append(
                    requests.Request("GET", f"{service_url}/exportImage", params=params).prepare().url
                )
                memfile = rasterio.MemoryFile(_export_strip(session, service_url, params))
                memfiles.append(memfile)
                datasets.append(memfile.open())
                left = right
            top = bottom
        mosaic, transform = merge(datasets, res=pixel, nodata=nodata)
        profile = {
            "driver": "GTiff",
            "dtype": mosaic.dtype.name,
            "count": mosaic.shape[0],
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "crs": datasets[0].crs,
            "transform": transform,
            "compress": compress,
            "tiled": True,
        }
        if nodata is not None:
            profile["nodata"] = nodata
        if compress == "jpeg":
            if mosaic.dtype != np.uint8 or mosaic.shape[0] != 3:
                raise ValueError("jpeg compression requires 3-band uint8 imagery")
            profile.update({"photometric": "YCBCR", "jpeg_quality": 90})
        elif np.issubdtype(mosaic.dtype, np.floating):
            profile["predictor"] = 3
        else:
            profile["predictor"] = 2
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp.tif")
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(mosaic)
        tmp.replace(target)
    finally:
        for dataset in datasets:
            dataset.close()
        for memfile in memfiles:
            memfile.close()
    return requested


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

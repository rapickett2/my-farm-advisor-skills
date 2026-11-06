#!/usr/bin/env python3
"""Offline smoke checks for reporting/generate_grower_web_map.py.

Run with a Python that has the data-pipeline requirements installed, e.g. the
runtime venv:

    "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \\
        my-farm-advisor/data-pipeline/src/scripts/tests/smoke_grower_web_map.py

Builds a temporary runtime with one synthetic grower (two farms, four fields,
soil summary for one field only), generates the map, and checks the embedded
polygons, popup metadata order, optional rows, size, determinism, and that no
raster/image data is embedded.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

SCRIPT_PATH = Path(__file__).resolve()
DATA_PIPELINE_DIR = SCRIPT_PATH.parents[3]
INSTALL_SCRIPT = DATA_PIPELINE_DIR / "scripts" / "install.sh"


class SmokeFailure(AssertionError):
    """Raised when a smoke check fails."""


def _farm(base: Path, farm: str, fields: list[tuple[str, str, float]]) -> Path:
    farm_dir = base / "growers" / "smoke-grower" / "farms" / farm
    rows = [
        {
            "field_id": field_id,
            "field_name": name,
            "grower_name": "Smoke Grower Inc",
            "farm_name": farm.replace("-", " ").title(),
            "state": "AR",
            "county_name": "Poinsett",
            "geometry": box(-90.80 + offset, 35.60, -90.796 + offset, 35.604),
        }
        for field_id, name, offset in fields
    ]
    (farm_dir / "boundary").mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(rows, crs="EPSG:4326").to_file(farm_dir / "boundary" / "field_boundaries.geojson")
    (farm_dir / "manifests").mkdir(parents=True, exist_ok=True)
    with (farm_dir / "manifests" / "field-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field_id", "field_slug"])
        for field_id, _, _ in fields:
            writer.writerow([field_id, field_id])
    return farm_dir


def _embedded(html_text: str) -> dict:
    match = re.search(r"const DATA = (.*?);\nconst FARMS", html_text, re.S)
    if not match:
        raise SmokeFailure("embedded GeoJSON not found")
    return json.loads(match.group(1).replace("<\\/", "</"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mfa-webmap-smoke-") as tmp:
        root = Path(tmp) / "runtime"
        env = os.environ.copy()
        env.update({
            "DATA_PIPELINE_DATA_ROOT": str(root),
            "DATA_PIPELINE_VENV_DIR": sys.prefix,
            "DATA_PIPELINE_BOOTSTRAP_NO_INSTALL_DEPS": "1",
        })
        subprocess.run(
            [str(INSTALL_SCRIPT), "--non-interactive", "--force-refresh", "--no-install-deps", "--persist", "none"],
            env=env, cwd=DATA_PIPELINE_DIR, check=True, capture_output=True,
        )
        base = root / "data-pipeline"
        north = _farm(base, "north-farm", [("f-10", "Field 10", 0.0), ("f-2", "Field 2", 0.01), ("f-1", "Field 1", 0.02)])
        _farm(base, "south-farm", [("s-1", "South </script><A>", 0.05)])
        soil = north / "fields" / "f-2" / "soil"
        soil.mkdir(parents=True, exist_ok=True)
        (soil / "ssurgo_summary.csv").write_text(
            "field_id,dominant_soil,drainage_class,avg_ph,avg_om_pct\nf-2,Sharkey,Poorly drained,6.1,2.3\n",
            encoding="utf-8",
        )
        # Two SSURGO map units splitting field f-2 (box -90.79..-90.786).
        gpd.GeoDataFrame(
            [{"mukey": "100", "geometry": box(-90.79, 35.60, -90.788, 35.604)},
             {"mukey": "200", "geometry": box(-90.788, 35.60, -90.786, 35.604)}],
            crs="EPSG:4326",
        ).to_file(soil / "ssurgo_soil_types.geojson")
        (soil / "ssurgo_full.csv").write_text(
            "field_id,mukey,compname,comppct_r,drainagecl,hzdept_r,hzdepb_r,om_r,ph1to1h2o_r,awc_r,claytotal_r,sandtotal_r\n"
            "f-2,100,Sharkey,85,Poorly drained,0,15,2.3,6.1,0.2,55,5\n"
            "f-2,100,Sharkey,85,Poorly drained,15,50,1.0,6.5,0.2,60,4\n"
            "f-2,100,Minor,15,Well drained,0,15,1.0,6.0,0.2,20,40\n"
            "f-2,200,Dundee,90,Somewhat poorly drained,0,20,1.4,5.8,0.21,22,18\n",
            encoding="utf-8",
        )
        # NDVI composites for field f-1 (box -90.78..-90.776): Soybeans 2021 (0.3), 2022 (0.5); 2024 Unknown.
        features_dir = north / "fields" / "f-1" / "derived" / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        years = []
        for year, crop, value in ((2021, "Soybeans", 0.3), (2022, "Soybeans", 0.5), (2024, "Unknown", 0.9)):
            tif = features_dir / f"ndvi_year_{year}_composite.tif"
            with rasterio.open(tif, "w", driver="GTiff", width=40, height=40, count=1, dtype="float32",
                               crs="EPSG:4326", transform=from_bounds(-90.7805, 35.5995, -90.7755, 35.6045, 40, 40),
                               nodata=-9999) as dst:
                dst.write(np.full((40, 40), value, dtype="float32"), 1)
            years.append({"year": year, "crop_name": crop, "scene_count": 9,
                          "composite_tif": str(tif.relative_to(base))})
        summaries = north / "fields" / "f-1" / "derived" / "summaries"
        summaries.mkdir(parents=True, exist_ok=True)
        (summaries / "ndvi_yearly_summary.json").write_text(json.dumps({"years": years}), encoding="utf-8")
        # Per-scene NDVI for the viewer: 2 Sentinel-2 + 1 Landsat clear scenes, 1 fully cloudy (skipped).
        satellite = north / "fields" / "f-1" / "satellite"
        for sensor, scenes in (
            ("sentinel", [("2021-06-01", 0.6), ("2021-04-01", 0.2), ("2022-07-01", np.nan)]),
            ("landsat", [("2022-05-01", 0.4)]),
        ):
            entries: dict[int, list] = {}
            for date, value in scenes:
                tif = satellite / sensor / date[:4] / f"{sensor}_{date}_ndvi.tif"
                tif.parent.mkdir(parents=True, exist_ok=True)
                with rasterio.open(tif, "w", driver="GTiff", width=20, height=20, count=1, dtype="float32",
                                   crs="EPSG:4326", transform=from_bounds(-90.7805, 35.5995, -90.7755, 35.6045, 20, 20),
                                   nodata=-9999) as dst:
                    dst.write(np.full((20, 20), -9999 if np.isnan(value) else value, dtype="float32"), 1)
                entries.setdefault(int(date[:4]), []).append(
                    {"scene_date": date, "cloud_cover": 0.05, "status": "complete", "ndvi_tif": str(tif.relative_to(base))})
            (satellite / sensor / "manifest.json").write_text(
                json.dumps({"years": [{"year": y, "scenes": s} for y, s in entries.items()]}), encoding="utf-8")
        (base / "growers" / "smoke-grower" / "grower.json").write_text('{"display_name": "smoke-grower"}', encoding="utf-8")

        command = [sys.executable, "scripts/reporting/generate_grower_web_map.py", "--all-growers"]
        result = subprocess.run(command, env=env, cwd=base / "src", capture_output=True, text=True)
        if result.returncode != 0:
            raise SmokeFailure(result.stdout + result.stderr)
        html_path = base / "growers" / "smoke-grower" / "derived" / "reports" / "smoke-grower_web_map.html"
        page = html_path.read_text(encoding="utf-8")
        data = _embedded(page)
        features = data["features"]
        if len(features) != 4 or any(f["geometry"]["type"] not in {"Polygon", "MultiPolygon"} for f in features):
            raise SmokeFailure(f"expected 4 polygon features, got {len(features)}")
        print("PASS: all fields from every farm embedded as real polygons")

        info = {f["properties"]["id"]: f["properties"]["info"] for f in features}
        first = info["north-farm/f-10"]
        if [row[0] for row in first[:3]] != ["Grower", "Farm", "Field"] or first[0][1] != "Smoke Grower Inc":
            raise SmokeFailure(f"popup must start with Grower/Farm/Field: {first[:3]}")
        if "Dominant soil" not in dict(info["north-farm/f-2"]) or "Dominant soil" in dict(first):
            raise SmokeFailure("optional soil rows must appear only where data exists")
        names = [f["properties"]["name"] for f in features if f["properties"]["farm"] == "north-farm"]
        if names != ["Field 1", "Field 2", "Field 10"]:
            raise SmokeFailure(f"fields not naturally sorted: {names}")
        print("PASS: popup metadata order, optional rows, natural field order")

        if "World_Imagery" not in page or "L.geoJSON" not in page or 'id="list"' not in page or "fitBounds" not in page:
            raise SmokeFailure("satellite basemap, polygons, field list, or fit control missing")
        if "World_Street_Map" not in page or "tile.openstreetmap.org" in page:
            raise SmokeFailure("street basemap must be Esri World Street Map (OSM tiles 403 on file:// pages)")
        okabe_ito = {"#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#F0E442", "#D55E00", "#0072B2"}
        farm_colors = re.search(r"const FARMS = (.*?);\n", page).group(1)
        if not set(re.findall(r"#[0-9A-Fa-f]{6}", farm_colors)) <= okabe_ito or "casingStyle" not in page:
            raise SmokeFailure("farm colours must come from the Okabe-Ito palette over a dark casing")
        # Data strings live inside JSON in a <script>; "</" must be escaped so they cannot close it.
        if page.count("</script>") != 2 or "South <\\/script>" not in page:
            raise SmokeFailure("embedded data can terminate the script block")
        # The main map embeds no images and no per-crop NDVI averages; NDVI lives in the viewer.
        if "data:image" in page or "ndvi-btn" in page or "const NDVI =" in page:
            raise SmokeFailure("main map must not embed NDVI images or average-NDVI buttons")
        if "viewer-link" not in page:
            raise SmokeFailure("field popups must link to the NDVI viewer")
        print("PASS: main map has no embedded images or NDVI averages; popups link to the viewer")

        soils = json.loads(re.search(r"const SOILS = (.*?);\n", page).group(1))["features"]
        legend = json.loads(re.search(r"const SOIL_LEGEND = (.*?);\n", page).group(1))
        if len(soils) != 2 or {s["name"] for s in legend["series"]} != {"Dundee", "Sharkey"}:
            raise SmokeFailure(f"soil units / legend wrong: {len(soils)} {legend['series']}")
        sharkey = next(s for s in soils if s["properties"]["series"] == "Sharkey")["properties"]
        info = dict(sharkey["info"])
        if info.get("Topsoil pH") != "6.1" or info.get("Drainage") != "Poorly drained" or "Area in field" not in info:
            raise SmokeFailure(f"soil popup must use the dominant component's topsoil: {info}")
        if [d["name"] for d in legend["drainage"]] != ["Poorly drained", "Somewhat poorly drained"]:
            raise SmokeFailure(f"drainage legend must be ordered wet to dry: {legend['drainage']}")
        if "SSURGO soils" not in page or "renderLegend" not in page:
            raise SmokeFailure("soils overlay or legend missing")
        print("PASS: SSURGO soil layer, dominant-component popups, series + drainage legends")
        if len(page.encode("utf-8")) > 200_000:
            raise SmokeFailure(f"map too large: {len(page)} bytes")

        viewer_path = html_path.with_name("smoke-grower_ndvi_viewer.html")
        if not viewer_path.exists() or 'const VIEWER_FILE = "smoke-grower_ndvi_viewer.html"' not in page:
            raise SmokeFailure("NDVI viewer not generated or not linked from the main map")
        viewer = viewer_path.read_text(encoding="utf-8")
        fields = json.loads(re.search(r"const FIELDS = (.*?);\nconst FARMS", viewer, re.S).group(1).replace("<\\/", "</"))
        f1 = next(f for f in fields if f["id"] == "north-farm/f-1")
        dates = [(s["d"], s["s"]) for s in f1["scenes"]]
        if dates != [("2021-04-01", "S2"), ("2021-06-01", "S2"), ("2022-05-01", "L")] or f1["skipped"] != 1:
            raise SmokeFailure(f"viewer scenes must be date-sorted, both sensors, cloudy skipped: {dates} {f1['skipped']}")
        if [round(s["mean"], 2) for s in f1["scenes"]] != [0.2, 0.6, 0.4] or set(f1["bounds"]) != {"S2", "L"}:
            raise SmokeFailure(f"viewer scene stats wrong: {f1['scenes']}")
        averages = f1["averages"]
        if [a["crop"] for a in averages] != ["Soybeans"] or averages[0]["years"] != [2021, 2022] or len(f1["composites"]) != 3:
            raise SmokeFailure(f"viewer averages must be per crop with Unknown years skipped: {averages}")
        if abs(averages[0]["mean"] - 0.4) > 0.01:
            raise SmokeFailure(f"average soybean NDVI should be 0.40, got {averages[0]['mean']}")
        meta = json.loads(re.search(r"const META = (.*?);\n", viewer).group(1))
        if (meta["min"], meta["max"]) != (0.0, 0.9) or "smoke-grower_web_map.html" not in viewer:
            raise SmokeFailure("viewer must use the fixed 0-0.9 scale and link back to the map")
        rgb = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (meta["colors"][0], meta["colors"][-1])]
        if not (rgb[0][0] > rgb[0][1] and rgb[1][1] > rgb[1][0]):
            raise SmokeFailure(f"NDVI ramp must run red (low) to green (high): {meta['colors']}")
        print("PASS: NDVI viewer — sorted scenes, cloudy skipped, per-crop averages (Unknown skipped), fixed red-to-green scale, links")
        print("PASS: Esri basemaps, colour-blind-safe cased outlines, field list, escaping, 200 KB budget")

        before = html_path.read_bytes()
        subprocess.run(command, env=env, cwd=base / "src", check=True, capture_output=True)
        if html_path.read_bytes() != before:
            raise SmokeFailure("regenerated map is not byte-identical")
        if not (base / "growers" / "web_maps_index.html").exists():
            raise SmokeFailure("index page missing")
        print("PASS: deterministic output and index page")
    return 0


if __name__ == "__main__":
    sys.exit(main())

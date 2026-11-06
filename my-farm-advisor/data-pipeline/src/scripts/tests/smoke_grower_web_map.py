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
        # Data strings live inside JSON in a <script>; "</" must be escaped so they cannot close it.
        if page.count("</script>") != 2 or "South <\\/script>" not in page:
            raise SmokeFailure("embedded data can terminate the script block")
        if "data:image" in page or "base64" in page:
            raise SmokeFailure("embedded image data found")
        if len(page.encode("utf-8")) > 200_000:
            raise SmokeFailure(f"map too large: {len(page)} bytes")
        print("PASS: Esri satellite + street basemaps, field list, escaping, size budget, no embedded imagery")

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

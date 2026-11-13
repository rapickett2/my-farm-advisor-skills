#!/usr/bin/env python3
"""Offline smoke test for run_field_eda.py on a synthetic runtime tree.

Run with the data-pipeline runtime venv (needs geopandas, rasterio, rasterstats,
scipy, matplotlib):

    "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \\
        my-farm-advisor/eda/field-eda/src/smoke_field_eda.py

No network: the map is drawn without basemap tiles.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class SmokeFailure(AssertionError):
    pass


UNITS = [
    # grower, farm, state, fips, county, prefix, origin lon, origin lat
    ("north-grower", "north-farm", "MO", "29", "Pemiscot", "NO", -89.86, 36.02),
    ("south-grower", "south-farm", "MS", "28", "Coahoma", "SO", -90.56, 34.36),
]


def _write_unit(base: Path, unit, sizes: list[float]) -> None:
    grower, farm, state, fips, county, prefix, lon0, lat0 = unit
    root = base / "growers" / grower / "farms" / farm
    (root / "boundary").mkdir(parents=True)
    (root / "manifests").mkdir(parents=True)
    features, inv = [], []
    for i, size in enumerate(sizes, 1):
        fid = f"{farm}-{prefix.lower()}-{i:02d}"
        geom = box(lon0 + i * 0.012, lat0, lon0 + i * 0.012 + size, lat0 + 0.006)
        features.append({"type": "Feature", "geometry": mapping(geom), "properties": {
            "field_id": fid, "field_name": f"{prefix} {i:02d}", "state": state, "state_fips": fips,
            "county_name": county}})
        inv.append((fid, fid))
        wdir = root / "fields" / fid / "weather"
        wdir.mkdir(parents=True)
        days = pd.date_range("2021-01-01", "2022-12-31", freq="D")
        warm = 2.0 if state == "MS" else 0.0
        seasonal = 12 * np.sin((days.dayofyear - 105) / 365 * 2 * np.pi)
        pd.DataFrame({
            "field_id": fid, "lat": lat0, "lon": lon0, "date": days.strftime("%Y-%m-%d"),
            "T2M": 16 + warm + seasonal, "T2M_MAX": 22 + warm + seasonal, "T2M_MIN": 10 + warm + seasonal,
            "PRECTOTCORR": 3.5, "ALLSKY_SFC_SW_DWN": 18, "RH2M": 70, "WS10M": 3,
        }).to_csv(wdir / "daily_weather.csv", index=False)
    (root / "boundary" / "field_boundaries.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    with (root / "manifests" / "field-inventory.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field_id", "field_slug"])
        w.writerows(inv)
    with (root / "manifests" / "field-attributes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field_id", "irrigation", "irrigation_source"])
        w.writerow([inv[0][0], "pivot", "user_confirmed"])


def _write_cdl(base: Path) -> None:
    """One EPSG:4326 raster per year/state: west half Corn (1), east half Soybeans (5)."""
    rasters = base / "shared" / "cdl" / "rasters"
    rasters.mkdir(parents=True)
    for _, _, _, fips, _, _, lon0, lat0 in UNITS:
        res = 0.0003
        width, height = 200, 40
        for year in (2021, 2022):
            arr = np.full((height, width), 1, dtype=np.uint8)
            arr[:, width // 2 + (year % 2) * 10:] = 5
            profile = {"driver": "GTiff", "width": width, "height": height, "count": 1, "dtype": "uint8",
                       "crs": "EPSG:4326", "transform": from_origin(lon0, lat0 + 0.01, res, res)}
            with rasterio.open(rasters / f"CDL_{year}_{fips}.tif", "w", **profile) as dst:
                dst.write(arr, 1)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="field-eda-smoke-") as tmp:
        root = Path(tmp)
        base = root / "data-pipeline"
        _write_unit(base, UNITS[0], [0.004, 0.006, 0.009])
        _write_unit(base, UNITS[1], [0.005, 0.007, 0.010])
        _write_cdl(base)
        os.environ["DATA_PIPELINE_DATA_ROOT"] = str(root)

        import run_field_eda

        out, pub = root / "out", root / "publish"
        rc = run_field_eda.main([
            "--unit", "north-grower:north-farm", "--unit", "south-grower:south-farm",
            "--cdl-years", "2021-2022", "--weather-years", "2021-2022",
            "--out-dir", str(out), "--publish-dir", str(pub), "--no-basemap",
        ])
        if rc != 0:
            raise SmokeFailure(f"run_field_eda returned {rc}")

        figures = sorted(p.name for p in (out / "figures").glob("*.png"))
        if len(figures) != 10:
            raise SmokeFailure(f"expected 10 figures (3 categories x 3 + map), got {figures}")
        print("PASS: 10 static figures (2 views + 1 comparison per category, plus map)")

        fields = pd.read_csv(out / "tables" / "fields.csv")
        if len(fields) != 6 or fields["pivot"].sum() != 2 or set(fields["grower"]) != {"MO", "MS"}:
            raise SmokeFailure(f"fields table wrong:\n{fields[['grower', 'field_name', 'pivot']]}")
        dom = pd.read_csv(out / "tables" / "cdl_majority_crop.csv")
        if len(dom) != 12 or not set(dom["crop"]) <= {"Corn", "Soybeans"}:
            raise SmokeFailure(f"CDL majority table wrong: {len(dom)} rows, {set(dom['crop'])}")
        season = pd.read_csv(out / "tables" / "season_weather.csv")
        ms, mo = (season.groupby("grower")["gdd_f"].mean()[k] for k in ("MS", "MO"))
        if not ms > mo:
            raise SmokeFailure("warmer synthetic MS weather must give more GDD than MO")
        print("PASS: tables — fields, pivots from field-attributes.csv, CDL majority, season GDD")

        page = (out / "field_eda_report.html").read_text(encoding="utf-8")
        import re

        text = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", page)
        if page.count("<img ") != 10 or re.search(r"\bnan\b", text, re.I) or "<script" in text.lower():
            raise SmokeFailure("report must embed 10 images, contain no NaN text and no scripts")
        if (out / "field_eda_report.html").stat().st_size > 1_000_000:
            raise SmokeFailure("report exceeds the 1 MB committed-asset limit")
        if not (pub / "field_eda_report.html").exists() or len(list((pub / "figures").glob("*.png"))) != 10:
            raise SmokeFailure("--publish-dir did not copy the report and figures")
        print("PASS: single self-contained static HTML report under 1 MB, published copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())

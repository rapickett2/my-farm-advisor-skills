#!/usr/bin/env python3
"""Offline smoke checks for reporting/compare_lidar_epochs.py.

Run with a Python that has the data-pipeline requirements installed, e.g. the
runtime venv:

    "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \\
        my-farm-advisor/data-pipeline/src/scripts/tests/smoke_compare_lidar_epochs.py

Builds a temporary runtime with two synthetic fields whose later surface is
offset +0.08 m farm-wide: one bumpy field planed flat (expect "likely
leveled") and one unchanged field (expect "no significant change").
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPT_PATH = Path(__file__).resolve()
DATA_PIPELINE_DIR = SCRIPT_PATH.parents[3]
INSTALL_SCRIPT = DATA_PIPELINE_DIR / "scripts" / "install.sh"
OFFSET = 0.08
SIZE_M = 300
BUFFER_M = 20


class SmokeFailure(AssertionError):
    """Raised when a smoke check fails."""


def _surface(rng: np.random.Generator, n: int, bumps: float) -> np.ndarray:
    yy, xx = np.mgrid[0:n, 0:n]
    plane = 75.0 + 0.0008 * xx
    relief = bumps * np.sin(xx / 25.0) * np.cos(yy / 30.0)
    return plane + relief + rng.normal(0, 0.02, (n, n))


def _write(path: Path, data: np.ndarray, x0: float, y0: float) -> None:
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "height": data.shape[0], "width": data.shape[1],
        "crs": "EPSG:26915", "transform": from_origin(x0, y0, 1.0, 1.0), "nodata": -9999.0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("float32"), 1)


def main() -> int:
    rng = np.random.default_rng(7)
    with tempfile.TemporaryDirectory(prefix="mfa-lidar-smoke-") as tmp:
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
        farm = base / "growers" / "smoke" / "farms" / "smoke-farm"
        n = SIZE_M + 2 * BUFFER_M
        fields = []
        cases = {"leveled": (0.25, 0.0), "steady": (0.10, 0.10)}
        for index, (slug, (early_bumps, late_bumps)) in enumerate(cases.items()):
            x0, y_top = 600000.0 + index * 1000, 3900000.0
            fields.append({
                "field_id": slug, "field_name": slug,
                "geometry": box(x0 + BUFFER_M, y_top - BUFFER_M - SIZE_M, x0 + BUFFER_M + SIZE_M, y_top - BUFFER_M),
            })
            early = _surface(rng, n, early_bumps)
            late = (early if late_bumps else _surface(rng, n, 0.0)) + OFFSET + rng.normal(0, 0.01, (n, n))
            if not late_bumps:
                # Only the field is planed; the surrounding ring keeps its relief.
                ring = np.ones((n, n), dtype=bool)
                ring[BUFFER_M:-BUFFER_M, BUFFER_M:-BUFFER_M] = False
                late[ring] = early[ring] + OFFSET
            lidar = farm / "fields" / slug / "terrain" / "state_lidar"
            _write(lidar / "2011_old.tif", early, x0, y_top)
            _write(lidar / "2026_new.tif", late, x0, y_top)
            manifest = {"collections": [
                {"epoch": e, "collection": c, "file": f, "pixel_size_m": 1.0, "field_coverage_pct": 100.0, "status": "ok"}
                for e, c, f in (("2011", "Old_2011", "2011_old.tif"), ("2026", "New_2026", "2026_new.tif"))
            ]}
            (lidar / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        gdf = gpd.GeoDataFrame(fields, crs="EPSG:26915").to_crs("EPSG:4326")
        (farm / "boundary").mkdir(parents=True, exist_ok=True)
        gdf.to_file(farm / "boundary" / "field_boundaries.geojson", driver="GeoJSON")
        (farm / "manifests").mkdir(parents=True, exist_ok=True)
        with (farm / "manifests" / "field-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["field_id", "field_slug"])
            for slug in cases:
                writer.writerow([slug, slug])

        result = subprocess.run(
            [sys.executable, "scripts/reporting/compare_lidar_epochs.py", "--grower", "smoke", "--farm", "smoke-farm"],
            env=env, cwd=base / "src", capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SmokeFailure(result.stdout + result.stderr)
        rows = {
            row["field_slug"]: row
            for row in csv.DictReader((farm / "derived" / "tables" / "smoke-farm_lidar_change_summary.csv").open())
        }
        bias = float(rows["steady"]["bias_m"])
        if abs(bias - OFFSET) > 0.01:
            raise SmokeFailure(f"farm bias {bias} != {OFFSET}")
        print("PASS: farm-wide bias recovered from field rings")
        if rows["leveled"]["verdict"] != "likely leveled":
            raise SmokeFailure(f"leveled field verdict: {rows['leveled']['verdict']}")
        if rows["steady"]["verdict"] != "no significant change":
            raise SmokeFailure(f"steady field verdict: {rows['steady']['verdict']}")
        print("PASS: leveled vs unchanged verdicts")
        for slug in cases:
            change = farm / "fields" / slug / "terrain" / "state_lidar" / "change"
            figure = farm / "fields" / slug / "derived" / "reports" / "lidar_change_2026_2011.png"
            if not (change / "2026_minus_2011_dz.tif").exists() or not figure.exists():
                raise SmokeFailure(f"missing outputs for {slug}")
        if not (farm / "derived" / "reports" / "smoke-farm_lidar_change.md").exists():
            raise SmokeFailure("missing farm markdown report")
        print("PASS: dZ rasters, figures, CSV, and markdown written")
    return 0


if __name__ == "__main__":
    sys.exit(main())

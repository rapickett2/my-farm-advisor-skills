#!/usr/bin/env python3
"""Offline smoke test for make_headlands_ring.py using a synthetic 200 m square field."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_headlands_ring as mhr  # noqa: E402

SIDE_M = 200.0
WIDTH_M = 21.0
EXPECTED_FIELD_SQM = SIDE_M**2
EXPECTED_RING_SQM = SIDE_M**2 - (SIDE_M - 2 * WIDTH_M) ** 2
REL_TOL = 0.002


def close(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= expected * REL_TOL


def write_square(path: Path, crs: str) -> None:
    # 200 m square in UTM 15N near Poinsett County, AR.
    square = gpd.GeoDataFrame(geometry=[box(700000, 3947000, 700000 + SIDE_M, 3947000 + SIDE_M)],
                              crs="EPSG:32615")
    square.to_crs(crs).to_file(path, driver="GPKG" if path.suffix == ".gpkg" else "GeoJSON")


def check_outputs(out_dir: Path) -> None:
    for name, expected in (("field_boundary.gpkg", EXPECTED_FIELD_SQM),
                           ("headlands_ring.gpkg", EXPECTED_RING_SQM)):
        gdf = gpd.read_file(out_dir / name)
        assert gdf.crs.to_epsg() == 4326, f"{name} CRS {gdf.crs}"
        assert len(gdf) == 1, f"{name} has {len(gdf)} rows"
        assert list(gdf.columns) == ["meters_squared", "acres", "geometry"], list(gdf.columns)
        sqm = float(gdf["meters_squared"].iloc[0])
        assert close(sqm, expected), f"{name} {sqm} vs {expected}"
        assert close(float(gdf["acres"].iloc[0]), expected * mhr.ACRES_PER_SQM)
    ring = gpd.read_file(out_dir / "headlands_ring.gpkg").geometry.iloc[0]
    assert ring.geom_type == "Polygon" and len(ring.interiors) == 1, "ring should have one hole"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        src = tmp_path / "square_4326.geojson"
        write_square(src, "EPSG:4326")
        out = tmp_path / "out_4326"
        assert mhr.main(["--input", str(src), "--out-dir", str(out)]) == 0
        check_outputs(out)

        before = (out / "headlands_ring.gpkg").stat().st_mtime_ns
        try:
            mhr.main(["--input", str(src), "--out-dir", str(out)])
        except FileExistsError:
            pass
        else:
            raise AssertionError("second run without --force should refuse to overwrite")
        assert (out / "headlands_ring.gpkg").stat().st_mtime_ns == before
        assert mhr.main(["--input", str(src), "--out-dir", str(out), "--force"]) == 0
        check_outputs(out)

        src_albers = tmp_path / "square_5070.gpkg"
        write_square(src_albers, "EPSG:5070")
        out_albers = tmp_path / "out_5070"
        assert mhr.main(["--input", str(src_albers), "--out-dir", str(out_albers)]) == 0
        check_outputs(out_albers)

        src_nocrs = tmp_path / "square_nocrs.gpkg"
        gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)]).to_file(src_nocrs, driver="GPKG")
        try:
            mhr.main(["--input", str(src_nocrs), "--out-dir", str(tmp_path / "out_nocrs")])
        except ValueError as exc:
            assert "no CRS" in str(exc)
        else:
            raise AssertionError("missing CRS should be rejected")

    print("smoke_make_headlands_ring: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

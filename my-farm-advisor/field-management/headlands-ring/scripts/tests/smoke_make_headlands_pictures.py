#!/usr/bin/env python3
"""Offline smoke test for make_headlands_pictures.py using synthetic fields."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import make_headlands_pictures as mhp  # noqa: E402
import make_headlands_ring as mhr  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def build_rings(tmp: Path, name: str, width_m: float, side_m: float) -> Path:
    square = gpd.GeoDataFrame(
        geometry=[box(700000, 3947000, 700000 + side_m, 3947000 + side_m)], crs="EPSG:32615"
    )
    src = tmp / f"{name}.geojson"
    square.to_crs("EPSG:4326").to_file(src, driver="GeoJSON")
    out = tmp / f"{name}_out"
    assert mhr.main(["--input", str(src), "--out-dir", str(out), "--width-m", str(width_m)]) == 0
    return out


def check_pngs(folder: Path, prefix: str) -> None:
    for suffix in ("overview", "workflow_steps"):
        png = folder / f"{prefix}_headlands_{suffix}.png"
        assert png.exists(), png
        data = png.read_bytes()
        assert data.startswith(PNG_MAGIC) and len(data) > 10_000, f"{png} looks empty"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        out = build_rings(tmp_path, "square", width_m=21, side_m=200)
        assert mhp.main(["--dir", str(out), "--title", "Test Field"]) == 0
        check_pngs(out, "test_field")

        before = (out / "test_field_headlands_overview.png").stat().st_mtime_ns
        try:
            mhp.main(["--dir", str(out), "--title", "Test Field"])
        except FileExistsError:
            pass
        else:
            raise AssertionError("second run without --force should refuse to overwrite")
        assert (out / "test_field_headlands_overview.png").stat().st_mtime_ns == before
        assert mhp.main(["--dir", str(out), "--title", "Test Field", "--force"]) == 0

        narrow = build_rings(tmp_path, "narrow", width_m=21, side_m=30)
        assert mhp.main(["--dir", str(narrow), "--prefix", "narrow"]) == 0
        check_pngs(narrow, "narrow")

        bad = tmp_path / "bad"
        bad.mkdir()
        gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs="EPSG:32615").to_file(
            bad / "field_boundary.gpkg", driver="GPKG"
        )
        gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs="EPSG:32615").to_file(
            bad / "headlands_ring.gpkg", driver="GPKG"
        )
        try:
            mhp.main(["--dir", str(bad)])
        except ValueError as exc:
            assert "EPSG:4326" in str(exc)
        else:
            raise AssertionError("non-4326 inputs should be rejected")

    print("smoke_make_headlands_pictures: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

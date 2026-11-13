"""Load data-pipeline runtime outputs into tidy field-level tables.

Every table is keyed by ``grower`` (short unit label) and ``field_id`` so the
plotting code never touches runtime paths. Only pipeline outputs are read:

    growers/<g>/farms/<f>/boundary/field_boundaries.geojson
    growers/<g>/farms/<f>/manifests/field-inventory.csv
    growers/<g>/farms/<f>/manifests/field-attributes.csv      (optional)
    growers/<g>/farms/<f>/fields/<slug>/weather/daily_weather.csv
    shared/cdl/rasters/CDL_<year>_<state_fips>.tif
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

EQUAL_AREA_CRS = "EPSG:5070"
ACRES_PER_M2 = 1 / 4046.8564224
MM_PER_INCH = 25.4

_SKILL_ROOT = Path(__file__).resolve().parents[3]
_CDL_SRC = _SKILL_ROOT / "soil" / "cdl-cropland" / "src"


@dataclass(frozen=True)
class FarmUnit:
    grower: str
    farm: str

    @classmethod
    def parse(cls, text: str) -> "FarmUnit":
        grower, _, farm = text.partition(":")
        if not grower or not farm:
            raise ValueError(f"expected <grower>:<farm>, got {text!r}")
        return cls(grower, farm)


def pipeline_root() -> Path:
    root = os.environ.get("DATA_PIPELINE_DATA_ROOT", "")
    if not root or not Path(root).is_absolute():
        sys.exit("ERROR: export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime")
    return Path(root) / "data-pipeline"


def farm_dir(base: Path, unit: FarmUnit) -> Path:
    return base / "growers" / unit.grower / "farms" / unit.farm


def _rectangularity(geom) -> float:
    rect = geom.minimum_rotated_rectangle
    return float(geom.area / rect.area) if rect.area else float("nan")


def _unit_label(fields: gpd.GeoDataFrame) -> tuple[str, str]:
    """Short display label, e.g. ('AR', 'Lee Co., AR'), from boundary properties."""
    state = str(fields["state"].mode().iat[0]) if "state" in fields else ""
    county = str(fields["county_name"].mode().iat[0]) if "county_name" in fields else ""
    place = ", ".join(p for p in (county and f"{county} Co.", state) if p)
    return state or fields["farm_slug"].iat[0], place


def load_fields(base: Path, units: list[FarmUnit]) -> gpd.GeoDataFrame:
    """One row per field with geometry (EPSG:5070) and shape metrics."""
    frames = []
    for unit in units:
        root = farm_dir(base, unit)
        gdf = gpd.read_file(root / "boundary" / "field_boundaries.geojson").to_crs(EQUAL_AREA_CRS)
        inventory = pd.read_csv(root / "manifests" / "field-inventory.csv", dtype=str)
        gdf = gdf.merge(inventory, on="field_id", how="left")
        attrs_path = root / "manifests" / "field-attributes.csv"
        if attrs_path.exists():
            attrs = pd.read_csv(attrs_path, dtype=str)
            gdf = gdf.merge(attrs, on="field_id", how="left")
        if "irrigation" not in gdf:
            gdf["irrigation"] = "unknown"
        gdf["irrigation"] = gdf["irrigation"].fillna("unknown")
        gdf["grower_slug"], gdf["farm_slug"] = unit.grower, unit.farm
        label, place = _unit_label(gdf)
        gdf["grower"], gdf["place"] = label, place
        frames.append(gdf)
    fields = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=EQUAL_AREA_CRS)

    fields["acres"] = fields.geometry.area * ACRES_PER_M2
    fields["perimeter_m"] = fields.geometry.length
    fields["edge_m_per_acre"] = fields["perimeter_m"] / fields["acres"]
    fields["rectangularity"] = [_rectangularity(g) for g in fields.geometry]
    fields["compactness"] = [
        4 * math.pi * g.area / g.length**2 if g.length else float("nan") for g in fields.geometry
    ]
    centroids = fields.geometry.centroid.to_crs("EPSG:4326")
    fields["lon"], fields["lat"] = centroids.x, centroids.y
    fields["pivot"] = fields["irrigation"].eq("pivot")
    keep = [
        "grower", "place", "grower_slug", "farm_slug", "field_id", "field_slug", "field_name",
        "state", "state_fips", "county_name", "irrigation", "pivot", "acres", "perimeter_m",
        "edge_m_per_acre", "rectangularity", "compactness", "lon", "lat", "geometry",
    ]
    return fields[[c for c in keep if c in fields.columns]].sort_values(
        ["grower", "field_name"]
    ).reset_index(drop=True)


def load_weather(base: Path, fields: gpd.GeoDataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    """Daily NASA POWER rows for every field, US units added."""
    frames = []
    for row in fields.itertuples():
        path = (
            base / "growers" / row.grower_slug / "farms" / row.farm_slug / "fields"
            / row.field_slug / "weather" / "daily_weather.csv"
        )
        if not path.exists():
            print(f"  Warning: no weather for {row.field_name} ({path})")
            continue
        df = pd.read_csv(path, parse_dates=["date"])
        df["field_id"], df["grower"], df["field_name"] = row.field_id, row.grower, row.field_name
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    weather = pd.concat(frames, ignore_index=True)
    weather = weather[weather["date"].dt.year.between(start_year, end_year)].copy()
    weather["year"] = weather["date"].dt.year
    weather["month"] = weather["date"].dt.month
    weather["precip_in"] = weather["PRECTOTCORR"] / MM_PER_INCH
    weather["tmax_f"] = weather["T2M_MAX"] * 9 / 5 + 32
    weather["tmin_f"] = weather["T2M_MIN"] * 9 / 5 + 32
    # Corn GDD, 86/50 °F method: cap Tmax at 86, floor Tmin at 50.
    tmax = weather["tmax_f"].clip(upper=86).clip(lower=50)
    tmin = weather["tmin_f"].clip(lower=50).clip(upper=86)
    weather["gdd_f"] = ((tmax + tmin) / 2 - 50).clip(lower=0)
    return weather


def load_cdl(base: Path, fields: gpd.GeoDataFrame, years: list[int]) -> pd.DataFrame:
    """Per-field CDL composition for each year, via the cdl-cropland zonal routine."""
    sys.path.insert(0, str(_CDL_SRC))
    from cdl_reporting import extract_crop_composition  # noqa: E402

    rows = []
    for (state_fips,), group in fields.groupby(["state_fips"]):
        group_4326 = group.to_crs("EPSG:4326")
        for year in years:
            raster = base / "shared" / "cdl" / "rasters" / f"CDL_{year}_{str(state_fips).zfill(2)}.tif"
            if not raster.exists():
                print(f"  Warning: missing {raster.name}; {year} skipped for state {state_fips}")
                continue
            comp = extract_crop_composition(group_4326[["field_id", "geometry"]], raster, year=year)
            rows.append(comp)
    if not rows:
        return pd.DataFrame()
    cdl = pd.concat(rows, ignore_index=True)
    meta = fields[["field_id", "grower", "field_name", "acres", "pivot"]]
    return cdl.merge(meta, on="field_id", how="left")


def dominant_crops(cdl: pd.DataFrame) -> pd.DataFrame:
    """One row per field-year: the majority crop and its share of the field."""
    if cdl.empty:
        return cdl
    top = cdl.sort_values("pct", ascending=False).groupby(["field_id", "year"], as_index=False).first()
    return top.rename(columns={"crop_name": "crop", "pct": "crop_pct"})[
        ["grower", "field_id", "field_name", "acres", "pivot", "year", "crop", "crop_pct"]
    ]


def season_weather(weather: pd.DataFrame, start_month: int = 4, end_month: int = 9) -> pd.DataFrame:
    """Growing-season totals per field-year (default April–September)."""
    season = weather[weather["month"].between(start_month, end_month)]
    agg = season.groupby(["grower", "field_id", "year"]).agg(
        gdd_f=("gdd_f", "sum"),
        precip_in=("precip_in", "sum"),
        hot_days=("tmax_f", lambda s: int((s >= 95).sum())),
        mean_tmax_f=("tmax_f", "mean"),
    )
    return agg.reset_index()


def within_unit_spread(table: pd.DataFrame, value: str) -> pd.DataFrame:
    """Max-minus-min of ``value`` across fields within each grower-year."""
    spread = table.groupby(["grower", "year"])[value].agg(lambda s: float(np.nanmax(s) - np.nanmin(s)))
    return spread.rename(f"{value}_field_range").reset_index()

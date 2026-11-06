#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Import grower-supplied field boundaries into canonical runtime farm paths.

Reads any vector file GeoPandas can open (shapefile, GeoPackage, GeoJSON) that
holds one row per field with grower, farm, and field-name columns. Each
grower/farm pair becomes a canonical farm under the runtime root:

    growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson
    growers/<grower>/farms/<farm>/manifests/field-inventory.csv
    growers/<grower>/farms/<farm>/manifests/user_upload_fields_provenance.json

Output is deterministic: fields are sorted, missing field IDs are derived from
the farm and field names, and re-running on the same source rewrites identical
files. Existing boundaries that differ are only replaced with --force.

Example:
    python scripts/ingest/import_boundaries.py \\
        --source "/path/to/Farm Boundaries.shp" --state-fips 05 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import pandas as pd
from shapely import force_2d
from shapely.validation import make_valid

from naming import slugify
from paths import (
    DATA_ROOT,
    SCRIPTS_ROOT,
    farm_boundary_path,
    farm_manifest_dir,
    shared_geoadmin_counties_dir,
)

SOURCE_LABEL = "user_upload"
ACRES_PER_M2 = 1 / 4046.8564224
EQUAL_AREA_CRS = "EPSG:5070"
COUNTIES_PATH = shared_geoadmin_counties_dir() / "counties_usa.geojson"


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _clean(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _polygonal(geom):
    """Drop Z/M, repair the geometry, and keep only its polygon parts."""
    if geom is None or geom.is_empty:
        return None
    # FMIS exports are often PolygonZ; USDA SDA silently returns no rows for
    # "POLYGON Z" WKT, so boundaries are always stored 2D.
    geom = force_2d(geom)
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        parts = [g for g in geom.geoms if g.geom_type in {"Polygon", "MultiPolygon"}]
        if not parts:
            return None
        geom = gpd.GeoSeries(parts).union_all()
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return geom


def _load_source(args: argparse.Namespace) -> gpd.GeoDataFrame:
    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        sys.exit(f"ERROR: source not found: {source}")
    gdf = gpd.read_file(source)
    if gdf.crs is None:
        print("  Warning: source has no CRS; assuming EPSG:4326")
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")

    columns = {
        "grower": args.grower_column,
        "farm": args.farm_column,
        "field_name": args.field_name_column,
    }
    missing = [col for col in columns.values() if col not in gdf.columns]
    if missing:
        sys.exit(f"ERROR: missing columns {missing}; available: {list(gdf.columns)}")

    out = gpd.GeoDataFrame(
        {
            "grower_name": gdf[args.grower_column].map(_clean),
            "farm_name": gdf[args.farm_column].map(_clean),
            "field_name": gdf[args.field_name_column].map(_clean),
            "source_field_id": (
                gdf[args.field_id_column].map(_clean)
                if args.field_id_column in gdf.columns
                else ""
            ),
        },
        geometry=gdf.geometry.map(_polygonal),
        crs="EPSG:4326",
    )
    dropped = out[out.geometry.isna() | (out["field_name"] == "")]
    for _, row in dropped.iterrows():
        print(f"  Warning: skipping row without polygon or field name: {row['field_name']!r}")
    out = out.drop(index=dropped.index)

    if args.grower:
        wanted = {slugify(value) for value in args.grower}
        out = out[out["grower_name"].map(slugify).isin(wanted)]
    if out.empty:
        sys.exit("ERROR: no fields left to import")
    return out


def _scoped_field_slug(farm_slug: str, field_name: str) -> str:
    """Prefix the field slug with the farm slug unless the name already carries it."""
    field_slug = slugify(field_name)
    if field_slug == farm_slug or field_slug.startswith(f"{farm_slug}-"):
        return field_slug
    return f"{farm_slug}-{field_slug}"


def _build_fields(gdf: gpd.GeoDataFrame, args: argparse.Namespace) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    gdf["grower_slug"] = gdf["grower_name"].map(slugify)
    gdf["farm_slug"] = gdf["farm_name"].map(slugify)
    # Purely numeric farm names make poor directory names on their own.
    gdf.loc[gdf["farm_slug"].str.fullmatch(r"\d+"), "farm_slug"] = "farm-" + gdf["farm_slug"]
    derived_ids = pd.Series(
        [_scoped_field_slug(f, n) for f, n in zip(gdf["farm_slug"], gdf["field_name"])],
        index=gdf.index,
    )
    gdf["field_id"] = gdf["source_field_id"].where(gdf["source_field_id"] != "", derived_ids)
    gdf["field_id_derived"] = gdf["source_field_id"] == ""

    # One feature per field: multi-row fields are merged into one (multi)polygon.
    keys = ["grower_slug", "farm_slug", "field_id"]
    gdf = gdf.dissolve(by=keys, aggfunc="first", as_index=False)

    gdf["field_slug"] = [
        _scoped_field_slug(f, n) for f, n in zip(gdf["farm_slug"], gdf["field_name"])
    ]
    duplicates = gdf[gdf.duplicated(subset=["grower_slug", "farm_slug", "field_slug"], keep=False)]
    if not duplicates.empty:
        # Same field name with different IDs: disambiguate deterministically.
        suffix = duplicates["field_id"].map(lambda value: slugify(value)[:8])
        gdf.loc[duplicates.index, "field_slug"] = duplicates["field_slug"] + "-" + suffix

    gdf["area_acres"] = (gdf.to_crs(EQUAL_AREA_CRS).geometry.area * ACRES_PER_M2).round(2)
    gdf["country"] = args.country.upper()
    gdf["state"] = args.state.upper() if args.state else ""
    gdf["state_fips"] = args.state_fips.zfill(2) if args.state_fips else ""
    gdf["source"] = SOURCE_LABEL
    gdf = _assign_counties(gdf)
    return gdf.sort_values(["grower_slug", "farm_slug", "field_slug"]).reset_index(drop=True)


def _assign_counties(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf["county_fips"] = ""
    gdf["county_name"] = ""
    if not COUNTIES_PATH.exists():
        print(f"  Note: county layer not found ({COUNTIES_PATH}); county_fips left blank")
        return gdf
    counties = gpd.read_file(COUNTIES_PATH).to_crs("EPSG:4326")
    points = gdf.geometry.representative_point()
    for idx, point in points.items():
        match = counties[counties.contains(point)]
        if match.empty:
            continue
        row = match.iloc[0]
        gdf.at[idx, "county_fips"] = _clean(row.get("county_fips")).zfill(3)
        gdf.at[idx, "county_name"] = _clean(row.get("county_name"))
        state_fips = _clean(row.get("state_fips")).zfill(2)
        if gdf.at[idx, "state_fips"] and state_fips != gdf.at[idx, "state_fips"]:
            print(
                f"  Warning: {gdf.at[idx, 'field_id']} falls in state FIPS {state_fips}, "
                f"not --state-fips {gdf.at[idx, 'state_fips']}"
            )
    return gdf


def _boundary_payload(fields: gpd.GeoDataFrame) -> str:
    columns = [
        "field_id",
        "field_name",
        "grower_name",
        "farm_name",
        "country",
        "state",
        "state_fips",
        "county_fips",
        "county_name",
        "area_acres",
        "source",
        "field_id_derived",
        "geometry",
    ]
    return fields[columns].to_json(drop_id=True, sort_keys=True)


def _write_farm(
    fields: gpd.GeoDataFrame, *, source: Path, source_sha256: str, force: bool
) -> tuple[str, Path]:
    grower_slug = fields["grower_slug"].iloc[0]
    farm_slug = fields["farm_slug"].iloc[0]
    boundary_path = farm_boundary_path(grower_slug, farm_slug)
    manifest_dir = farm_manifest_dir(grower_slug, farm_slug)
    payload = _boundary_payload(fields)

    if boundary_path.exists() and boundary_path.read_text(encoding="utf-8") != payload and not force:
        sys.exit(
            f"ERROR: {boundary_path} exists with different content; rerun with --force to replace"
        )

    boundary_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    boundary_path.write_text(payload, encoding="utf-8")

    inventory_path = manifest_dir / "field-inventory.csv"
    with inventory_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field_id", "field_slug"])
        for _, row in fields.iterrows():
            writer.writerow([row["field_id"], row["field_slug"]])

    provenance = {
        "source": SOURCE_LABEL,
        "source_path": str(source),
        "source_sha256": source_sha256,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "grower_slug": grower_slug,
        "farm_slug": farm_slug,
        "farm_name": fields["farm_name"].iloc[0],
        "field_count": len(fields),
        "boundary_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    (manifest_dir / f"{SOURCE_LABEL}_fields_provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    return payload, inventory_path


def _print_summary(fields: gpd.GeoDataFrame) -> None:
    for (grower_slug, farm_slug), farm in fields.groupby(["grower_slug", "farm_slug"], sort=True):
        print(
            f"\n{grower_slug} / {farm_slug} ({farm['farm_name'].iloc[0]}): "
            f"{len(farm)} fields, {farm['area_acres'].sum():.1f} acres"
        )
        for _, row in farm.iterrows():
            flag = " (derived id)" if row["field_id_derived"] else ""
            county = f" {row['county_name']}" if row["county_name"] else ""
            print(
                f"  {row['field_slug']:<28} {row['area_acres']:>9.2f} ac{county}  "
                f"{row['field_id']}{flag}"
            )


def _run_pipeline(grower_slug: str, farm_slug: str, farm_name: str, force: bool) -> None:
    cmd = [
        sys.executable,
        str(SCRIPTS_ROOT / "run_farm_pipeline.py"),
        "--grower-slug",
        grower_slug,
        "--farm-slug",
        farm_slug,
        "--farm-name",
        farm_name,
    ]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, cwd=str(DATA_ROOT), check=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="Boundary file (.shp, .gpkg, .geojson)")
    parser.add_argument("--grower-column", default="CLIENT_NAM")
    parser.add_argument("--farm-column", default="FARM_NAME")
    parser.add_argument("--field-name-column", default="FIELD_NAME")
    parser.add_argument(
        "--field-id-column",
        default="FIELD_ID",
        help="Optional; blank or missing IDs are derived as <farm-slug>-<field-name-slug>",
    )
    parser.add_argument("--country", default="US", help="ISO-2 country written to boundaries")
    parser.add_argument("--state", default="", help="State/province abbreviation, e.g. AR")
    parser.add_argument(
        "--state-fips",
        default="",
        help="Two-digit US state FIPS (required for US so CDL uses the right state)",
    )
    parser.add_argument(
        "--grower",
        action="append",
        help="Only import this grower name (repeatable; matched by slug)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the import plan only")
    parser.add_argument("--force", action="store_true", help="Replace differing boundaries")
    parser.add_argument(
        "--run-pipeline", action="store_true", help="Run run_farm_pipeline.py for each farm"
    )
    args = parser.parse_args(argv)

    if args.country.upper() == "US" and not args.state_fips:
        parser.error("--state-fips is required when --country is US")

    source = Path(args.source).expanduser().resolve()
    fields = _build_fields(_load_source(args), args)
    _print_summary(fields)

    if args.dry_run:
        print("\nDry run: no files written.")
        return

    source_sha256 = _sha256_file(source)
    farms = []
    for _, farm in fields.groupby(["grower_slug", "farm_slug"], sort=True):
        _write_farm(farm, source=source, source_sha256=source_sha256, force=args.force)
        farms.append(
            (farm["grower_slug"].iloc[0], farm["farm_slug"].iloc[0], farm["farm_name"].iloc[0])
        )
        print(f"\nWrote {farm_boundary_path(farms[-1][0], farms[-1][1])}")

    if args.run_pipeline:
        for grower_slug, farm_slug, farm_name in farms:
            _run_pipeline(grower_slug, farm_slug, farm_name, args.force)


if __name__ == "__main__":
    main()

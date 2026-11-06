#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Compare lidar DEM epochs per field to detect land leveling and earthwork.

Reads the clips written by ingest/download_state_lidar.py. For each field it
picks the best collection per epoch (highest field coverage, then finest
pixel), and for every epoch pair (later minus earlier):

1. Aligns both surfaces on the coarser grid (finer surface cell-averaged).
2. Computes dZ = Z_later - Z_earlier inside the field.
3. Estimates one vertical bias per farm and collection pair as the median dZ
   over the rings just outside all fields (datum/geoid or processing offsets
   are farm-wide; real earthwork is local), and reports both raw and
   bias-corrected statistics. Each field's own ring median is kept as a
   diagnostic: when it departs from the farm bias, the field edge itself
   (ditch, road, levee) changed.
4. Computes cut/fill volumes beyond a noise threshold, changed area, and for
   each surface the best-fit-plane slope and roughness (residual std) and the
   90th-percentile local slope.
5. Assigns a transparent rule-based verdict (thresholds are written out).

Outputs (runtime tree):
    fields/<field>/terrain/state_lidar/change/<later>_minus_<earlier>_dz.tif
    fields/<field>/terrain/state_lidar/change/<later>_minus_<earlier>.json
    fields/<field>/derived/reports/lidar_change_<later>_<earlier>.png
    derived/tables/<farm>_lidar_change_summary.csv
    derived/reports/<farm>_lidar_change.md
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.colors import LightSource, TwoSlopeNorm
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject

from paths import (
    farm_boundary_path,
    farm_manifest_dir,
    farm_reports_dir,
    farm_tables_dir,
    field_reports_dir,
    field_terrain_dir,
)
from reporting_bootstrap import field_slug_map_from_inventory

NODATA = -9999.0
M3_TO_YD3 = 1.307950619
M3_PER_ACRE_FOOT = 1233.48184
M2_PER_ACRE = 4046.8564224
RING_M = 15.0

# Verdict thresholds (documented in every output).
NOISE_M = 0.15
LEVELED_CHANGED_PCT = 20.0
LEVELED_ROUGHNESS_RATIO = 0.6
WORK_CHANGED_PCT = 5.0
WORK_ROUGHNESS_RATIO = 0.8
# Roughness ratios only count when the earlier surface had relief to remove.
MIN_EARLY_ROUGHNESS_M = 0.05
# Already-smooth graded fields re-leveled to zero grade (rice) cannot show a
# roughness drop; recognize the end state instead.
ZERO_GRADE_ROUGHNESS_M = 0.025
ZERO_GRADE_SLOPE_PCT = 0.02
BIAS_WARN_M = 0.10
EDGE_CHANGE_WARN_M = 0.10
FLAT_PATCH_WARN_PCT = 20.0
ELEVATION_SPIKE_WARN_PCT = 8.0
# Roughness and grade are measured on the field interior so perimeter levees,
# borders, and ditches (often rebuilt during leveling) do not mask it.
INTERIOR_INSET_M = 10.0
ELEVATION_SPIKE_RATIO = 2.5


def _best_per_epoch(manifest: dict) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for entry in manifest.get("collections", []):
        if entry.get("status", "ok") != "ok" or not entry.get("file"):
            continue
        key = (-float(entry.get("field_coverage_pct") or 0), float(entry["pixel_size_m"]), entry["collection"])
        current = best.get(entry["epoch"])
        if current is None or key < current["_key"]:
            best[entry["epoch"]] = {**entry, "_key": key}
    return {epoch: best[epoch] for epoch in sorted(best) if epoch != "unknown"}


def _read_on_grid(path: Path, grid: dict) -> np.ndarray:
    out = np.full((grid["height"], grid["width"]), np.nan, dtype="float32")
    with rasterio.open(path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_nodata=src.nodata,
            dst_transform=grid["transform"],
            dst_crs=grid["crs"],
            dst_nodata=np.nan,
            resampling=Resampling.average if src.res[0] < grid["res"] - 1e-6 else Resampling.nearest,
        )
    out[out <= NODATA + 1] = np.nan
    return out


def _plane_stats(z: np.ndarray, mask: np.ndarray, res: float) -> dict:
    rows, cols = np.nonzero(mask & np.isfinite(z))
    if rows.size < 10:
        return {"plane_slope_pct": None, "roughness_m": None, "slope_p90_pct": None, "flat_patch_pct": None}
    x = cols * res
    y = -rows * res
    values = z[rows, cols].astype("float64")
    design = np.column_stack([np.ones_like(x), x, y])
    coef, *_ = np.linalg.lstsq(design, values, rcond=None)
    residual = values - design @ coef
    gy, gx = np.gradient(np.where(np.isfinite(z), z, np.nan), res)
    slope = np.hypot(gx, gy) * 100.0
    local_slope = slope[rows, cols]
    # Lidar over standing water is interpolated and nearly perfectly flat.
    padded = np.pad(np.where(np.isfinite(z), z, np.nan), 1, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (3, 3))
    local_std = np.nanstd(windows, axis=(-1, -2))
    flat = (local_std[rows, cols] < 0.003).mean() * 100.0
    # Water surfaces and void fills also show up as one elevation holding many cells.
    counts, edges = np.histogram(values, bins=np.arange(values.min(), values.max() + 0.02, 0.02))
    spike = counts.max() / values.size * 100.0 if counts.size else 0.0
    # Share a smooth (Gaussian) surface with this spread would put in its busiest 2 cm bin.
    expected = min(100.0, 0.02 / (max(values.std(), 1e-3) * np.sqrt(2 * np.pi)) * 100.0)
    return {
        "plane_slope_pct": round(float(np.hypot(coef[1], coef[2]) * 100.0), 3),
        "roughness_m": round(float(residual.std()), 4),
        "slope_p90_pct": round(float(np.nanpercentile(local_slope, 90)), 3),
        "flat_patch_pct": round(float(flat), 1),
        "elevation_spike_pct": round(float(spike), 1),
        "elevation_spike_ratio": round(float(spike / expected), 2),
        "elevation_spike_m": round(float(edges[counts.argmax()] + 0.01), 2) if counts.size else None,
    }


def _verdict(stats: dict) -> str:
    ratio = stats.get("roughness_ratio")
    if (stats.get("early_roughness_m") or 0) < MIN_EARLY_ROUGHNESS_M:
        ratio = None
    changed = stats["changed_pct"]
    if ratio is not None and ratio <= LEVELED_ROUGHNESS_RATIO and changed >= LEVELED_CHANGED_PCT:
        return "likely leveled"
    late_rough = stats.get("late_roughness_m")
    late_slope = stats.get("late_plane_slope_pct")
    if (
        changed >= LEVELED_CHANGED_PCT
        and late_rough is not None
        and late_rough <= ZERO_GRADE_ROUGHNESS_M
        and late_slope is not None
        and late_slope <= ZERO_GRADE_SLOPE_PCT
    ):
        return "likely leveled (zero grade)"
    edge = stats.get("edge_dz_m")
    median = stats["median_dz_m"]
    if (
        changed >= WORK_CHANGED_PCT
        and edge is not None
        and abs(edge) > EDGE_CHANGE_WARN_M
        and np.sign(edge) == np.sign(median)
        and abs(median - edge) < NOISE_M
        and (ratio is None or ratio > WORK_ROUGHNESS_RATIO)
    ):
        # Field and its surroundings moved together without smoothing: a local
        # flight-line/calibration offset, not earthwork.
        return "uniform shift (check data)"
    if changed >= LEVELED_CHANGED_PCT:
        return "field-wide earthwork (re-graded?)"
    if changed >= WORK_CHANGED_PCT or (ratio is not None and ratio <= WORK_ROUGHNESS_RATIO):
        return "localized work"
    return "no significant change"


def _hillshade(z: np.ndarray, res: float) -> np.ndarray:
    filled = np.where(np.isfinite(z), z, np.nanmean(z))
    return LightSource(azdeg=315, altdeg=45).hillshade(filled, vert_exag=3, dx=res, dy=res)


def _render(path: Path, field_name: str, early: dict, late: dict, z0, z1, dz, inside, grid, stats) -> None:
    extent = (
        grid["transform"].c,
        grid["transform"].c + grid["width"] * grid["res"],
        grid["transform"].f - grid["height"] * grid["res"],
        grid["transform"].f,
    )
    outline = grid["outline"]
    fig, axes = plt.subplots(2, 2, figsize=(13, 10))
    for ax, z, entry in ((axes[0, 0], z0, early), (axes[0, 1], z1, late)):
        ax.imshow(_hillshade(z, grid["res"]), cmap="gray", extent=extent)
        outline.boundary.plot(ax=ax, color="red", linewidth=1)
        ax.set_title(f"{entry['epoch']} hillshade — {entry['collection']} ({entry['pixel_size_m']} m)")
    shown = np.where(inside, dz, np.nan)
    limit = max(0.3, float(np.nanpercentile(np.abs(shown), 98))) if np.isfinite(shown).any() else 0.3
    im = axes[1, 0].imshow(shown, cmap="RdBu", norm=TwoSlopeNorm(0, -limit, limit), extent=extent)
    outline.boundary.plot(ax=axes[1, 0], color="black", linewidth=1)
    fig.colorbar(im, ax=axes[1, 0], label="ΔZ (m): red = cut, blue = fill")
    axes[1, 0].set_title(f"ΔZ {late['epoch']} − {early['epoch']} (farm bias {stats['bias_m']:+.3f} m removed)")
    base = np.nanmean(z0[inside])
    bins = np.linspace(np.nanpercentile(z0[inside] - base, 0.5) - 0.2, np.nanpercentile(z0[inside] - base, 99.5) + 0.2, 60)
    axes[1, 1].hist((z0[inside] - base)[np.isfinite(z0[inside])], bins=bins, alpha=0.6, label=early["epoch"])
    axes[1, 1].hist((z1[inside] - stats["bias_m"] - base)[np.isfinite(z1[inside])], bins=bins, alpha=0.6, label=late["epoch"])
    axes[1, 1].set_xlabel(f"Elevation relative to {early['epoch']} field mean (m)")
    axes[1, 1].set_ylabel("Cells")
    axes[1, 1].legend()
    axes[1, 1].set_title(
        f"Roughness {stats['early_roughness_m']} → {stats['late_roughness_m']} m; "
        f"plane slope {stats['early_plane_slope_pct']} → {stats['late_plane_slope_pct']} %"
    )
    for ax in axes.flat[:3]:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(
        f"{field_name}: {stats['verdict'].upper()} — changed {stats['changed_pct']}% of field, "
        f"cut {stats['cut_yd3']:,.0f} yd³ / fill {stats['fill_yd3']:,.0f} yd³ (|ΔZ| > {NOISE_M} m)",
        fontsize=12,
    )
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _align(field, lidar_dir: Path, early: dict, late: dict) -> dict:
    early_path, late_path = lidar_dir / early["file"], lidar_dir / late["file"]
    coarse = early_path if early["pixel_size_m"] >= late["pixel_size_m"] else late_path
    with rasterio.open(coarse) as src:
        grid = {
            "transform": src.transform,
            "crs": src.crs,
            "width": src.width,
            "height": src.height,
            "res": src.res[0],
        }
    z0 = _read_on_grid(early_path, grid)
    z1 = _read_on_grid(late_path, grid)
    outline = gpd.GeoSeries([field.geometry], crs="EPSG:4326").to_crs(grid["crs"])
    grid["outline"] = outline
    shape = (grid["height"], grid["width"])
    inside = geometry_mask([outline.iloc[0]], shape, grid["transform"], invert=True)
    ring_geom = outline.iloc[0].buffer(RING_M).difference(outline.iloc[0].buffer(2.0))
    ring = geometry_mask([ring_geom], shape, grid["transform"], invert=True)
    interior_geom = outline.iloc[0].buffer(-INTERIOR_INSET_M)
    interior = (
        geometry_mask([interior_geom], shape, grid["transform"], invert=True)
        if not interior_geom.is_empty
        else inside
    )
    if interior.sum() < 0.2 * inside.sum():
        interior = inside
    dz_raw = z1 - z0
    return {
        "z0": z0, "z1": z1, "dz_raw": dz_raw, "inside": inside, "interior": interior, "grid": grid,
        "shape": shape, "ring_dz": dz_raw[ring & np.isfinite(dz_raw)],
    }


def _compare(
    field, field_slug: str, lidar_dir: Path, reports_dir: Path, early: dict, late: dict,
    aligned: dict, bias: float,
) -> dict:
    z0, z1, dz_raw = aligned["z0"], aligned["z1"], aligned["dz_raw"]
    inside, grid, shape = aligned["inside"], aligned["grid"], aligned["shape"]
    valid = inside & np.isfinite(dz_raw)
    ring_dz = aligned["ring_dz"]
    field_ring_bias = float(np.nanmedian(ring_dz)) if ring_dz.size else None
    dz = dz_raw - bias
    cell_area = grid["res"] ** 2
    field_dz = dz[valid]
    fill = float(field_dz[field_dz > NOISE_M].sum() * cell_area) + 0.0
    cut = float(-field_dz[field_dz < -NOISE_M].sum() * cell_area) + 0.0
    field_acres = valid.sum() * cell_area / M2_PER_ACRE
    interior = aligned["interior"]
    early_stats = _plane_stats(z0, interior, grid["res"])
    late_stats = _plane_stats(z1, interior, grid["res"])

    stats = {
        "field_id": str(field["field_id"]),
        "field_slug": field_slug,
        "field_name": str(field.get("field_name", field_slug)),
        "early_epoch": early["epoch"],
        "early_collection": early["collection"],
        "late_epoch": late["epoch"],
        "late_collection": late["collection"],
        "grid_res_m": round(grid["res"], 3),
        "compared_acres": round(field_acres, 2),
        "median_dz_raw_m": round(float(np.nanmedian(dz_raw[valid])), 3),
        "bias_m": round(bias, 3),
        "field_ring_dz_m": round(field_ring_bias, 3) if field_ring_bias is not None else None,
        "edge_dz_m": round(field_ring_bias - bias, 3) if field_ring_bias is not None else None,
        "median_dz_m": round(float(np.nanmedian(field_dz)), 3),
        "p05_dz_m": round(float(np.nanpercentile(field_dz, 5)), 3),
        "p95_dz_m": round(float(np.nanpercentile(field_dz, 95)), 3),
        "changed_pct": round(100.0 * (np.abs(field_dz) > NOISE_M).mean(), 1),
        "cut_m3": round(float(cut), 1),
        "fill_m3": round(float(fill), 1),
        "cut_yd3": round(float(cut) * M3_TO_YD3, 1),
        "fill_yd3": round(float(fill) * M3_TO_YD3, 1),
        "net_acre_ft": round(float(fill - cut) / M3_PER_ACRE_FOOT, 3),
        "earthwork_yd3_per_acre": round(float(cut + fill) * M3_TO_YD3 / 2 / field_acres, 1) if field_acres else None,
        "cut_fill_balance": round(min(cut, fill) / max(cut, fill), 2) + 0.0 if max(cut, fill) > 0 else None,
    }
    for prefix, values in (("early", early_stats), ("late", late_stats)):
        stats.update({f"{prefix}_{key}": value for key, value in values.items()})
    if stats["early_roughness_m"] and stats["late_roughness_m"] is not None:
        stats["roughness_ratio"] = round(stats["late_roughness_m"] / stats["early_roughness_m"], 3)
    else:
        stats["roughness_ratio"] = None
    stats["verdict"] = _verdict(stats)
    flags = []
    if abs(bias) > BIAS_WARN_M:
        flags.append(f"farm bias {bias:+.2f} m removed (datum/processing offset?)")
    if field_ring_bias is not None and abs(field_ring_bias - bias) > EDGE_CHANGE_WARN_M:
        flags.append(f"field edge changed {field_ring_bias - bias:+.2f} m vs farm (ditch/road/levee work?)")
    for prefix in ("early", "late"):
        if (stats.get(f"{prefix}_flat_patch_pct") or 0) > FLAT_PATCH_WARN_PCT:
            flags.append(f"{stats[f'{prefix}_epoch']} has {stats[f'{prefix}_flat_patch_pct']}% flat patches (standing water?)")
        if (
            (stats.get(f"{prefix}_elevation_spike_pct") or 0) > ELEVATION_SPIKE_WARN_PCT
            and (stats.get(f"{prefix}_elevation_spike_ratio") or 0) >= ELEVATION_SPIKE_RATIO
        ):
            flags.append(
                f"{stats[f'{prefix}_epoch']}: {stats[f'{prefix}_elevation_spike_pct']}% of cells at "
                f"{stats[f'{prefix}_elevation_spike_m']} m ±1 cm (zero-grade surface, standing water, "
                f"or void fill — check imagery)"
            )
    stats["flags"] = "; ".join(flags)
    stats["thresholds"] = (
        f"noise {NOISE_M} m; leveled = roughness ratio <= {LEVELED_ROUGHNESS_RATIO} and changed >= "
        f"{LEVELED_CHANGED_PCT}%; field-wide earthwork = changed >= {LEVELED_CHANGED_PCT}% otherwise; "
        f"localized work = changed >= {WORK_CHANGED_PCT}% or ratio <= {WORK_ROUGHNESS_RATIO}; "
        f"ratios ignored when earlier roughness < {MIN_EARLY_ROUGHNESS_M} m; zero grade = changed >= "
        f"{LEVELED_CHANGED_PCT}%, later interior roughness <= {ZERO_GRADE_ROUGHNESS_M} m and plane slope <= "
        f"{ZERO_GRADE_SLOPE_PCT}%; roughness/slope on the field interior ({INTERIOR_INSET_M:g} m inset); "
        f"uniform shift = field and edge "
        f"moved together (> {EDGE_CHANGE_WARN_M} m, within {NOISE_M} m of each other) without smoothing"
    )

    tag = f"{late['epoch']}_minus_{early['epoch']}"
    change_dir = lidar_dir / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "height": shape[0], "width": shape[1],
        "crs": grid["crs"], "transform": grid["transform"], "nodata": NODATA, "compress": "deflate",
    }
    with rasterio.open(change_dir / f"{tag}_dz.tif", "w", **profile) as dst:
        dst.write(np.where(inside & np.isfinite(dz), dz, NODATA).astype("float32"), 1)
    (change_dir / f"{tag}.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    _render(reports_dir / f"lidar_change_{late['epoch']}_{early['epoch']}.png", stats["field_name"],
            early, late, z0, z1, dz, inside, grid, stats)
    return stats


def _write_markdown(path: Path, farm_slug: str, table: pd.DataFrame) -> None:
    lines = [
        f"# Lidar elevation change — {farm_slug}",
        "",
        "dZ = later minus earlier surface. One farm-wide vertical bias per collection pair (median dZ over",
        "the rings just outside all fields) is removed; per-field edge change is the field's own ring median",
        "minus that bias. Roughness = std of residuals from a best-fit plane (leveling drives it toward the",
        "lidar noise floor, ~0.02 m). The 90th-percentile local slope is in the CSV only: older 1 m lidar is",
        "noisier than cell-averaged 0.5 m lidar, so it drops across all fields and is not used in verdicts.",
        "Inter-flight artifacts (straight swath/tile seams, stripes) of 0.05–0.2 m are common: always check",
        "the ΔZ panel — straight N–S/E–W edges are data seams, curved or field-shaped patterns are earthwork.",
        f"Thresholds: {table['thresholds'].iloc[0]}.",
        "",
    ]
    for (early_name, late_name), group in table.groupby(["early_collection", "late_collection"], sort=True):
        lines.append(f"Farm bias {early_name} → {late_name}: {group['bias_m'].iloc[0]:+.3f} m")
    lines += [
        "",
        "| Field | Epochs | Verdict | Changed % | Cut yd³ | Fill yd³ | yd³/ac | Cut/fill balance | Roughness m (before→after) | Plane slope % (before→after) | Edge Δ m | Flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, row in table.iterrows():
        edge = row["edge_dz_m"] if pd.notna(row["edge_dz_m"]) else float("nan")
        lines.append(
            f"| {row['field_name']} | {row['early_epoch']}→{row['late_epoch']} | {row['verdict']} | "
            f"{row['changed_pct']} | {row['cut_yd3']:,.0f} | {row['fill_yd3']:,.0f} | {row['earthwork_yd3_per_acre']} | "
            f"{row['cut_fill_balance']} | {row['early_roughness_m']}→{row['late_roughness_m']} | "
            f"{row['early_plane_slope_pct']}→{row['late_plane_slope_pct']} | {edge:+.3f} | {row['flags'] or ''} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--grower", default=os.environ.get("AG_GROWER_SLUG", "default-grower"))
    parser.add_argument("--farm", default=os.environ.get("AG_FARM_SLUG", "default-farm"))
    parser.add_argument("--field", action="append", help="Only these field slugs (repeatable)")
    args = parser.parse_args(argv)

    fields = gpd.read_file(farm_boundary_path(args.grower, args.farm)).to_crs("EPSG:4326")
    slugs = field_slug_map_from_inventory(farm_manifest_dir(args.grower, args.farm) / "field-inventory.csv")
    jobs = []
    for _, field in fields.sort_values("field_id").iterrows():
        field_slug = slugs.get(str(field["field_id"]))
        if not field_slug or (args.field and field_slug not in args.field):
            continue
        lidar_dir = field_terrain_dir(args.grower, args.farm, field_slug) / "state_lidar"
        manifest_path = lidar_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"skip  {field_slug}: no state_lidar manifest (run ingest/download_state_lidar.py)")
            continue
        epochs = _best_per_epoch(json.loads(manifest_path.read_text(encoding="utf-8")))
        if len(epochs) < 2:
            print(f"skip  {field_slug}: fewer than two epochs ({', '.join(epochs) or 'none'})")
            continue
        for early_epoch, late_epoch in itertools.combinations(sorted(epochs), 2):
            jobs.append((field, field_slug, lidar_dir, epochs[early_epoch], epochs[late_epoch]))

    # Pass 1: one bias per collection pair from all field rings (deterministic subsample).
    ring_samples: dict[tuple[str, str], list[np.ndarray]] = {}
    for field, _, lidar_dir, early, late in jobs:
        ring_dz = _align(field, lidar_dir, early, late)["ring_dz"]
        step = max(1, ring_dz.size // 20000)
        ring_samples.setdefault((early["collection"], late["collection"]), []).append(ring_dz[::step])
    biases = {
        key: float(np.median(np.concatenate(parts))) if sum(p.size for p in parts) else 0.0
        for key, parts in ring_samples.items()
    }
    for (early_name, late_name), bias in sorted(biases.items()):
        print(f"farm bias {early_name} -> {late_name}: {bias:+.3f} m")

    # Pass 2: per-field statistics, rasters, and figures.
    rows = []
    for field, field_slug, lidar_dir, early, late in jobs:
        aligned = _align(field, lidar_dir, early, late)
        reports_dir = field_reports_dir(args.grower, args.farm, field_slug)
        bias = biases[(early["collection"], late["collection"])]
        stats = _compare(field, field_slug, lidar_dir, reports_dir, early, late, aligned, bias)
        rows.append(stats)
        print(
            f"  {field_slug:<24} {early['epoch']}->{late['epoch']}  {stats['verdict']:<26} "
            f"changed {stats['changed_pct']:>5}%  cut {stats['cut_yd3']:>9,.0f}  fill {stats['fill_yd3']:>9,.0f} yd3  "
            f"rough {stats['early_roughness_m']}->{stats['late_roughness_m']} m  edge {stats['edge_dz_m']:+.3f}"
        )
    if not rows:
        sys.exit("No field had two or more lidar epochs to compare.")
    table = pd.DataFrame(rows).sort_values(["field_slug", "early_epoch", "late_epoch"])
    csv_path = farm_tables_dir(args.grower, args.farm) / f"{args.farm}_lidar_change_summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(csv_path, index=False)
    md_path = farm_reports_dir(args.grower, args.farm) / f"{args.farm}_lidar_change.md"
    _write_markdown(md_path, args.farm, table)
    print(f"\nSummary: {csv_path}\nReport:  {md_path}")


if __name__ == "__main__":
    main()

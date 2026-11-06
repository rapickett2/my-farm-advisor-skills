#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Generate a lightweight interactive web map for each grower in the runtime tree.

Reads only data-pipeline outputs: every farm's canonical
``boundary/field_boundaries.geojson`` plus optional per-field summaries (soil,
crop history, terrain, NDVI, lidar change) when they exist. Writes one
self-contained HTML file per grower with the field polygons on an ESRI World
Imagery satellite basemap (loaded online via Leaflet), click popups with
grower / farm / field metadata, a field list that zooms to each field, and a
"fit all" control. No rasters or data bundles are embedded; polygons are
simplified (~0.5 m) and rounded to 1e-6 degrees. Output is deterministic:
same pipeline data in, same HTML out.

Outputs (runtime tree):
    growers/<grower>/derived/reports/<grower>_web_map.html
    growers/web_maps_index.html
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import shapely

from paths import (
    GROWERS_ROOT,
    farm_boundary_path,
    farm_cdl_rotation_path,
    farm_manifest_dir,
    farm_tables_dir,
    field_manifest_dir,
    field_soil_summary_path,
    field_summaries_dir,
    field_tables_dir,
    grower_dir,
)
from reporting_bootstrap import field_slug_map_from_inventory

EQUAL_AREA_CRS = "EPSG:5070"
ACRES_PER_M2 = 1 / 4046.8564224
SIMPLIFY_M = 0.5
SIZE_WARN_BYTES = 1_000_000
FARM_COLORS = ["#ffd400", "#00e5ff", "#ff6ec7", "#7CFC00", "#ff8c00", "#b388ff", "#ff5252", "#64ffda"]
LEAFLET_VERSION = "1.9.4"


# --------------------------------------------------------------------------- data


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_csv(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _num(value, digits: int = 1) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return f"{number:,.{digits}f}"


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text))]


def _soil(grower: str, farm: str, field_slug: str) -> dict:
    rows = _read_csv(field_soil_summary_path(grower, farm, field_slug))
    if not rows:
        return {}
    row = rows[0]
    out = {}
    if row.get("dominant_soil"):
        out["Dominant soil"] = row["dominant_soil"]
    if row.get("drainage_class"):
        out["Drainage"] = row["drainage_class"]
    ph, om = _num(row.get("avg_ph")), _num(row.get("avg_om_pct"))
    if ph or om:
        out["Topsoil pH / OM"] = f"{ph or '–'} / {om + ' %' if om else '–'}"
    return out


def _crop_history(rotation_rows: dict, field_id: str) -> dict:
    row = rotation_rows.get(field_id)
    if not row or not row.get("rotation_sequence"):
        return {}
    years = f"{row.get('history_start_year', '')}–{row.get('history_end_year', '')}".strip("–")
    return {f"Crop history ({years})" if years else "Crop history": row["rotation_sequence"].replace(" -> ", " → ")}


def _terrain(grower: str, farm: str, field_slug: str) -> dict:
    summary = _read_json(field_tables_dir(grower, farm, field_slug) / "dem_terrain_summary.json")
    manifest = _read_json(field_manifest_dir(grower, farm, field_slug) / "dem_terrain_manifest.json")
    out = {}
    elevation = summary.get("elevation_range_m") or {}
    lo, hi = _num(elevation.get("min"), 2), _num(elevation.get("max"), 2)
    if lo and hi:
        out["Elevation (field + 20 m buffer)"] = f"{lo}–{hi} m"
    source = manifest.get("selected_source") or {}
    if source.get("source_name"):
        resolution = source.get("resolution_m")
        out["Terrain source"] = source["source_name"] + (f" ({resolution:g} m)" if resolution else "")
    return out


def _ndvi(grower: str, farm: str, field_slug: str) -> dict:
    summary = _read_json(field_summaries_dir(grower, farm, field_slug) / "ndvi_yearly_summary.json")
    years = [entry for entry in summary.get("years", []) if entry.get("year")]
    if not years:
        return {}
    scenes = sum(int(entry.get("scene_count") or 0) for entry in years)
    span = f"{min(e['year'] for e in years)}–{max(e['year'] for e in years)}"
    return {"NDVI archive": f"{len(years)} seasons ({span}), {scenes} scenes"}


def _lidar_change(change_rows: dict, field_id: str) -> dict:
    rows = change_rows.get(field_id)
    if not rows:
        return {}
    row = max(rows, key=lambda r: (r.get("late_epoch", ""), -int(r.get("early_epoch") or 0)))
    out = {
        f"Lidar change {row['early_epoch']}→{row['late_epoch']}": (
            f"{row['verdict']} ({row.get('changed_pct', '?')} % of field changed)"
        )
    }
    grade = _num(row.get("late_plane_slope_pct"), 3)
    if grade:
        out[f"Field grade ({row['late_epoch']} lidar)"] = f"{grade} %"
    return out


def _farm_features(grower: str, farm_path: Path, grower_label: str | None) -> tuple[list[dict], dict]:
    farm = farm_path.name
    boundary = farm_boundary_path(grower, farm)
    if not boundary.exists():
        return [], {}
    gdf = gpd.read_file(boundary)
    if gdf.empty:
        return [], {}
    gdf = gdf.to_crs("EPSG:4326")
    farm_meta = _read_json(farm_path / "farm.json")
    slugs = field_slug_map_from_inventory(farm_manifest_dir(grower, farm) / "field-inventory.csv")
    rotation = {r["field_id"]: r for r in _read_csv(farm_cdl_rotation_path(grower, farm)) if r.get("field_id")}
    changes: dict[str, list[dict]] = {}
    for row in _read_csv(farm_tables_dir(grower, farm) / f"{farm}_lidar_change_summary.csv"):
        changes.setdefault(row.get("field_id", ""), []).append(row)

    projected = gdf.to_crs(EQUAL_AREA_CRS)
    simplified = projected.geometry.simplify(SIMPLIFY_M, preserve_topology=True).to_crs("EPSG:4326")
    features = []
    farm_name = None
    for idx, row in gdf.iterrows():
        field_id = str(row.get("field_id", idx))
        field_slug = slugs.get(field_id, field_id)
        field_name = str(row.get("field_name") or row.get("name") or field_slug)
        farm_name = str(row.get("farm_name") or farm_meta.get("display_name") or farm)
        grower_name = str(row.get("grower_name") or grower_label or grower)
        acres = row.get("area_acres")
        if acres is None or acres != acres:
            acres = projected.geometry.loc[idx].area * ACRES_PER_M2
        county = str(row.get("county_name") or "").strip()
        state = str(row.get("state") or "").strip()
        props = {
            "Grower": grower_name,
            "Farm": farm_name,
            "Field": field_name,
            "Field ID": field_id,
            "Acres": _num(acres, 1),
        }
        if county or state:
            props["County / State"] = ", ".join(part for part in (county and f"{county} Co.", state) if part)
        for extra in (
            _soil(grower, farm, field_slug),
            _crop_history(rotation, field_id),
            _ndvi(grower, farm, field_slug),
            _terrain(grower, farm, field_slug),
            _lidar_change(changes, field_id),
        ):
            props.update(extra)
        geometry = shapely.set_precision(simplified.loc[idx], 1e-6)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": f"{farm}/{field_slug}",
                    "farm": farm,
                    "name": field_name,
                    "acres": float(acres),
                    # Ordered [label, value] pairs: popup rows keep this order.
                    "info": [[key, value] for key, value in props.items() if value],
                },
                "geometry": shapely.geometry.mapping(geometry),
            }
        )
    features.sort(key=lambda f: _natural_key(f["properties"]["name"]))
    return features, {"slug": farm, "name": farm_name or farm}


# --------------------------------------------------------------------------- html

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.js" crossorigin=""></script>
<style>
  html, body { height: 100%; margin: 0; font: 14px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  #app { display: flex; height: 100%; }
  #side { width: 290px; display: flex; flex-direction: column; background: #12161c; color: #e8eaed; box-shadow: 2px 0 8px rgba(0,0,0,.4); z-index: 1000; }
  #side header { padding: 14px 16px 10px; border-bottom: 1px solid #2a313b; }
  #side h1 { font-size: 17px; margin: 0 0 4px; }
  #side .stats { color: #9aa4b2; font-size: 12.5px; }
  #side .tools { display: flex; gap: 6px; margin-top: 10px; }
  #side input { flex: 1; padding: 6px 8px; border-radius: 6px; border: 1px solid #333c48; background: #1b2129; color: #e8eaed; }
  #side button { padding: 6px 10px; border-radius: 6px; border: 0; background: #2f6fed; color: #fff; cursor: pointer; }
  #side button:hover { background: #4a84f5; }
  #list { overflow-y: auto; flex: 1; padding: 4px 0 12px; }
  .farm { padding: 10px 16px 4px; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: #9aa4b2; display: flex; align-items: center; gap: 8px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .item { padding: 6px 16px 6px 34px; cursor: pointer; display: flex; justify-content: space-between; gap: 8px; }
  .item:hover, .item.active { background: #1f2a38; }
  .item small { color: #9aa4b2; white-space: nowrap; }
  #side footer { padding: 8px 16px; font-size: 11px; color: #7d8794; border-top: 1px solid #2a313b; }
  #map { flex: 1; }
  .leaflet-popup-content { margin: 10px 12px; min-width: 230px; }
  .popup h3 { margin: 0 0 6px; font-size: 15px; }
  .popup table { border-collapse: collapse; font-size: 12.5px; }
  .popup td { padding: 2px 6px 2px 0; vertical-align: top; }
  .popup td:first-child { color: #555; white-space: nowrap; }
  .field-label { background: rgba(0,0,0,.55); color: #fff; border: 0; box-shadow: none; font-weight: 600; padding: 1px 5px; }
  .field-label::before { display: none; }
  .labels-off .field-label { display: none; }
  @media (max-width: 700px) { #side { width: 200px; } }
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header>
      <h1>__HEADING__</h1>
      <div class="stats">__STATS__</div>
      <div class="tools"><input id="filter" placeholder="Filter fields…" aria-label="Filter fields"><button id="fit" title="Zoom to all fields">Fit all</button></div>
    </header>
    <div id="list"></div>
    <footer>Boundaries: My Farm Advisor data pipeline. Imagery: Esri World Imagery (Esri, Maxar, Earthstar Geographics).</footer>
  </aside>
  <div id="map"></div>
</div>
<script>
const DATA = __DATA__;
const FARMS = __FARMS__;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const map = L.map("map", { zoomControl: true, preferCanvas: false });
const imagery = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }).addTo(map);
const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });
const labels = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Labels &copy; Esri" });
L.control.scale({ imperial: true, metric: true }).addTo(map);

const color = slug => (FARMS.find(f => f.slug === slug) || {}).color || "#ffd400";
const baseStyle = f => ({ color: color(f.properties.farm), weight: 2, fillColor: color(f.properties.farm), fillOpacity: 0.12 });
const layers = {};
const popupHtml = p => `<div class="popup"><h3>${esc(p.name)}</h3><table>${
  p.info.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table></div>`;

const fields = L.geoJSON(DATA, {
  style: baseStyle,
  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    layers[p.id] = layer;
    layer.bindPopup(popupHtml(p), { maxWidth: 360, autoPanPadding: [30, 30] });
    layer.bindTooltip(esc(p.name), { permanent: true, direction: "center", className: "field-label" });
    layer.on("mouseover", () => layer.setStyle({ weight: 4, fillOpacity: 0.3 }));
    layer.on("mouseout", () => layer.setStyle(baseStyle(feature)));
    layer.on("click", () => select(p.id, false));
  }
}).addTo(map);

L.control.layers({ "Satellite (Esri)": imagery, "Streets (OSM)": streets }, { "Place labels": labels, "Fields": fields },
  { collapsed: false }).addTo(map);

const fitAll = () => map.fitBounds(fields.getBounds(), { padding: [30, 30] });
const updateLabels = () => document.getElementById("map").classList.toggle("labels-off", map.getZoom() < 15);
map.on("zoomend", updateLabels);
fitAll();
updateLabels();

function select(id, zoom = true) {
  document.querySelectorAll(".item").forEach(el => el.classList.toggle("active", el.dataset.id === id));
  const layer = layers[id];
  if (!layer) return;
  if (zoom) {
    // Open after the zoom animation so auto-pan can fit the popup on screen.
    map.once("moveend", () => layer.openPopup());
    map.fitBounds(layer.getBounds(), { paddingTopLeft: [40, 260], paddingBottomRight: [40, 40], maxZoom: 18 });
  } else {
    layer.openPopup();
  }
  const el = document.querySelector(`.item[data-id="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

const list = document.getElementById("list");
FARMS.forEach(farm => {
  const head = document.createElement("div");
  head.className = "farm";
  head.innerHTML = `<span class="swatch" style="background:${farm.color}"></span>${esc(farm.name)}`;
  list.appendChild(head);
  DATA.features.filter(f => f.properties.farm === farm.slug).forEach(f => {
    const item = document.createElement("div");
    item.className = "item";
    item.dataset.id = f.properties.id;
    item.dataset.search = f.properties.name.toLowerCase();
    item.innerHTML = `<span>${esc(f.properties.name)}</span><small>${f.properties.acres.toFixed(1)} ac</small>`;
    item.onclick = () => select(f.properties.id);
    list.appendChild(item);
  });
});
document.getElementById("fit").onclick = fitAll;
document.getElementById("filter").oninput = e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(".item").forEach(el => { el.style.display = el.dataset.search.includes(q) ? "" : "none"; });
};
</script>
</body>
</html>
"""


def _json_for_html(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).replace("</", "<\\/")


def build_grower_map(grower: str) -> tuple[Path, dict]:
    grower_root = grower_dir(grower)
    grower_meta = _read_json(grower_root / "grower.json")
    grower_label = grower_meta.get("display_name") if grower_meta.get("display_name") != grower else None
    features: list[dict] = []
    farms: list[dict] = []
    for farm_path in sorted((grower_root / "farms").iterdir(), key=lambda p: _natural_key(p.name)):
        if not farm_path.is_dir():
            continue
        farm_features, farm_info = _farm_features(grower, farm_path, grower_label)
        if not farm_features:
            continue
        farm_info["color"] = FARM_COLORS[len(farms) % len(FARM_COLORS)]
        farms.append(farm_info)
        features.extend(farm_features)
    if not features:
        raise RuntimeError(f"grower {grower} has no farm boundaries")

    grower_name = dict(features[0]["properties"]["info"]).get("Grower", grower)
    total_acres = sum(f["properties"]["acres"] for f in features)
    stats = (
        f"{len(farms)} farm{'s' if len(farms) != 1 else ''} · {len(features)} fields · "
        f"{total_acres:,.0f} acres"
    )
    page = (
        _TEMPLATE.replace("__LEAFLET__", LEAFLET_VERSION)
        .replace("__TITLE__", html.escape(f"{grower_name} — field map"))
        .replace("__HEADING__", html.escape(grower_name))
        .replace("__STATS__", html.escape(stats))
        .replace("__FARMS__", _json_for_html(farms))
        .replace("__DATA__", _json_for_html({"type": "FeatureCollection", "features": features}))
    )
    target = grower_root / "derived" / "reports" / f"{grower}_web_map.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target, {
        "grower": grower,
        "name": grower_name,
        "farms": len(farms),
        "fields": len(features),
        "acres": total_acres,
        "bytes": target.stat().st_size,
    }


def write_index(entries: list[dict]) -> Path:
    rows = "\n".join(
        f'<li><a href="{html.escape(e["grower"])}/derived/reports/{html.escape(e["grower"])}_web_map.html">'
        f'{html.escape(e["name"])}</a> — {e["farms"]} farm(s), {e["fields"]} fields, {e["acres"]:,.0f} acres</li>'
        for e in sorted(entries, key=lambda e: _natural_key(e["name"]))
    )
    page = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Grower web maps</title>'
        "<style>body{font:15px system-ui,sans-serif;margin:40px;max-width:720px}li{margin:8px 0}</style>"
        f"</head><body><h1>Grower web maps</h1><ul>\n{rows}\n</ul></body></html>\n"
    )
    target = GROWERS_ROOT / "web_maps_index.html"
    target.write_text(page, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grower", action="append", help="Grower slug (repeatable)")
    group.add_argument("--all-growers", action="store_true", help="Every grower under growers/")
    args = parser.parse_args(argv)

    growers = (
        sorted(p.name for p in GROWERS_ROOT.iterdir() if (p / "farms").is_dir())
        if args.all_growers
        else args.grower
    )
    if not growers:
        sys.exit("No growers found under the runtime growers/ directory.")
    entries = []
    for grower in growers:
        target, entry = build_grower_map(grower)
        entries.append(entry)
        warn = "  WARNING: over 1 MB" if entry["bytes"] > SIZE_WARN_BYTES else ""
        print(
            f"ok    {grower:<22} {entry['farms']} farm(s), {entry['fields']:>3} fields, "
            f"{entry['bytes'] / 1024:,.0f} KB  {target}{warn}"
        )
    if args.all_growers:
        print(f"index {write_index(entries)}")


if __name__ == "__main__":
    main()

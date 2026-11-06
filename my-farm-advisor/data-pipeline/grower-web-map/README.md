# Grower Web Map (data-pipeline subskill)

Generates one interactive HTML map per grower from the data-pipeline runtime tree, so a grower's
fields can be reviewed, shared, or demoed in any modern browser. Created for Assignment 1 as a new
subskill; it does not depend on `admin/interactive-web-map`.

## What the map shows

| Feature | Detail |
|---|---|
| Field polygons | Every field of every farm for the grower, read from `farms/<farm>/boundary/field_boundaries.geojson` (the pipeline's canonical boundaries), labelled when zoomed in (≥ z15) |
| Colour and contrast | Farms use the colour-blind-safe **Okabe–Ito** palette (orange, sky blue, bluish green, reddish purple, yellow, vermillion, blue). Each outline is a coloured line over a dark casing, so it stays visible on dark satellite imagery and on the pale street map; fill is slightly stronger on Streets. The selected field is marked by a thicker dashed outline (not by colour alone); labels are white on a dark, white-bordered tag |
| Basemap | ESRI World Imagery satellite tiles (online); switch to ESRI World Street Map; optional ESRI place-label overlay. All tiles come from `server.arcgisonline.com`, which serves pages opened from disk. OpenStreetMap's standard tile servers are deliberately not used: their tile policy returns HTTP 403 "Access blocked" to `file://` pages (no Referer header), which is how downloaded maps are viewed |
| Navigation | Zoom/pan, scale bar, **Fit all** button, sidebar field list grouped by farm (natural sort) that zooms to a field and opens its popup, text filter |
| Click a field | Popup with Grower, Farm, Field, Field ID, Acres, County / State, then any of the optional rows below that exist, and an **Average NDVI by crop** section |
| SSURGO soils (overlay, off by default) | Each field's SSURGO map units (`fields/<field>/soil/ssurgo_soil_types.geojson`) under the field outlines, coloured by **soil series** (Paul Tol "muted" colour-blind-safe palette, Water in cyan) or by **drainage class** (cividis, dark blue = wet to yellow = dry). Click a unit for series, share of map unit, drainage, topsoil pH / OM / clay / sand / AWC (dominant component, shallowest horizon from `soil/ssurgo_full.csv`), map unit key, and acres / % of the field. While soils are on, field fills turn off so units are clickable |
| Average NDVI by crop | For each field, the yearly NDVI composites (`derived/features/ndvi_year_<year>_composite.tif`) are grouped by that year's crop (`ndvi_yearly_summary.json`, from CDL) and averaged pixel by pixel; years labelled "Unknown" are skipped (the default CDL history ends in 2023). Popup buttons ("Average Soybeans NDVI · 2021, 2023 · mean 0.35") overlay that field's map at native 10 m; **Show for all fields** compares every field for that crop |
| Legend | Bottom-right panel that appears with the active layer: soil series with drainage and acres (Series / Drainage switch), and the NDVI ramp (classic **red → yellow → green**, matplotlib `RdYlGn`: red = low NDVI, green = high) with years, scenes, and field mean. Red–green ramps are hard for red-green colour-blind viewers; the soil layer and field outlines remain colour-blind safe (change `NDVI_COLORMAP` to e.g. `viridis` for an accessible ramp). The NDVI ramp is stretched to the grower's 2nd–98th percentile (rounded to 0.05), shared by every field on that map |

## NDVI detail viewer (companion page)

`generate_grower_web_map.py` also writes `growers/<grower>/derived/reports/<grower>_ndvi_viewer.html`
(skip with `--no-ndvi-viewer`; standalone: `generate_grower_ndvi_viewer.py --all-growers`). Each field
popup on the map has **Open NDVI viewer ↗**, which opens the viewer with that field selected
(`#field=<farm>/<field>`); download both files into the same folder for the link to work.

| Feature | Detail |
|---|---|
| Scenes | Every field's Sentinel-2 (10 m) and Landsat (30 m) NDVI scenes from `fields/<field>/satellite/{sentinel,landsat}/manifest.json`, all archive seasons, sorted by date; scenes with < 10 % clear pixels inside the field are skipped, 10–50 % are labelled "partly cloudy" |
| Flip through | ◀ / ▶ buttons and arrow keys, date slider, ▶ Play (space), opacity slider; year tabs (All / each season), sensor filter, "Hide partly cloudy" |
| Per-scene info | Date, sensor, field-mean NDVI, CDL crop for that year, % of field clear, scene cloud cover, position (n / total) |
| Chart | Field-mean NDVI over the season (Mar–Nov), one line per year (Okabe–Ito colours), filled = Sentinel-2, hollow = Landsat; click a point to jump to that scene |
| Averages tab | Average NDVI per crop (same method as the map) and each year's season composite |
| Colour | Red → yellow → green on a **fixed 0–0.9 scale** so colours stay comparable while flipping (the map's per-crop averages use a per-grower stretch instead) |
| Size | Field-clipped native-resolution palette PNGs (~1–3 KB each): typically 1–3 MB per grower; warns above 8 MB. The main map stays lightweight |

Optional popup rows (skipped silently when the pipeline step has not produced them):

| Row | Source file (under the field or farm folder) |
|---|---|
| Dominant soil, Drainage, Topsoil pH / OM | `fields/<field>/soil/ssurgo_summary.csv` |
| Crop history (years) | `derived/tables/<farm>_crop_rotation.csv` |
| NDVI archive (seasons, scenes) | `fields/<field>/derived/summaries/ndvi_yearly_summary.json` |
| Elevation (field + 20 m buffer), Terrain source | `fields/<field>/derived/tables/dem_terrain_summary.json`, `fields/<field>/manifests/dem_terrain_manifest.json` |
| Lidar change verdict, Field grade | `derived/tables/<farm>_lidar_change_summary.csv` (opt-in `compare_lidar_epochs.py`) |

Grower, farm, and state come from the boundary properties (`grower_name`, `farm_name`, `state`)
when present, falling back to `grower.json` / `farm.json` display names and the slugs. `farm.json`
`state` is not used: the canonical-tree bootstrap writes a placeholder there.

## Size and determinism

- Embedded: vector polygons (fields, soil units; simplified ~0.5 m, topology preserving, in
  EPSG:5070, rounded to 1e-6°), short text, and the per-field average-NDVI-by-crop maps — small
  field-clipped, native 10 m, colourised PNGs (a few KB each). No raw imagery, aerial photos, other
  rasters, or data bundles. Typical output is 95–125 KB per grower; the script warns above 1 MB and
  the smoke test enforces 500 KB.
- Leaflet 1.9.4 and the basemap tiles load from the internet at view time.
- Same pipeline data in, same HTML out: fields are sorted, JSON keys are sorted, and no run
  timestamp is written.

## Usage

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
PY="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"
"$PY" scripts/reporting/generate_grower_web_map.py --all-growers
"$PY" scripts/reporting/generate_grower_web_map.py --grower mfp --grower wrightland-brothers
```

The script is part of the data-pipeline `src/` tree, so `scripts/install.sh` seeds it into the
runtime copy like every other pipeline script. It is a standalone step and is not called by
`run_farm_pipeline.py`; run it after the pipeline (or after adding fields) to refresh the maps.

To view from a remote workspace, download the HTML (VS Code: right-click → Download) and open it in
Firefox or Chrome.

## Validation

- Offline: `src/scripts/tests/smoke_grower_web_map.py` (synthetic two-farm grower in a temporary
  runtime; checks polygons, metadata, field list, size, determinism, no raster embedding).
- Repository: `./scripts/validate.sh` from the repo root.

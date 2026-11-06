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
| Click a field | Popup with Grower, Farm, Field, Field ID, Acres, County / State, then any of the optional rows below that exist |

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

- Only vector polygons and short text are embedded: geometries are simplified (~0.5 m, topology
  preserving, in EPSG:5070) and rounded to 1e-6°. No rasters, imagery, or data bundles. Typical
  output is 15–60 KB per grower; the script warns above 1 MB.
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

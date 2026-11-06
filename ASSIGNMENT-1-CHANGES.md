# Assignment 1 — change log

Branch `assignment-1` on the public fork `rapickett2/my-farm-advisor-skills`, compared with `main`
(upstream `borealBytes/my-farm-advisor-skills` at `4a63876`). All work was planned in OpenCode
**Plan mode** and built in **Build mode**.

**Instructor-approved substitution:** the assignment's Illinois and Iowa demo growers were replaced
with three real Arkansas growers from my own field boundaries (35 fields, ~1,350 acres). Every
required step was completed against that data.

| Grower | Farm | Fields | County |
|---|---|---|---|
| `asu-precision-ag` (ASU-Precision AG) | `nerrec` (NERREC) | 21 | Poinsett, AR |
| `mfp` (MFP) | `farm-1723` (1723) | 6 | Poinsett, AR |
| `wrightland-brothers` (Wrightland Brothers) | `wrightland` (Wrightland) | 8 | Lee, AR |

## Changes on the branch (committed)

### 1. Grower boundary importer — new
- **What:** `data-pipeline/src/scripts/ingest/import_boundaries.py` imports any shapefile, GeoPackage,
  or GeoJSON (one row per field) into canonical pipeline farms: boundary GeoJSON,
  `field-inventory.csv`, and a provenance record with the source file's sha256. Repairs and merges
  geometries, computes acres (EPSG:5070), adds county FIPS/name, derives stable IDs for blank
  `FIELD_ID`s, writes `state_fips`, sorts output (byte-identical re-imports), `--dry-run`, `--force`.
- **Why:** the pipeline only seeded sampled OSM/CSB demo fields; I needed my own boundaries.
- **Docs/tests:** `data-pipeline/README.md`, `data-pipeline/AGENTS.md`,
  `references/field-boundary-sources.md` (`source=user_upload`), `tests/smoke_import_boundaries.py`.
- **Commit:** `eb68e5e`

### 2. Fix: imported boundaries are written 2D — bug fix
- **What:** the importer drops Z/M coordinates.
- **Why:** my FMIS shapefile is PolygonZ; USDA Soil Data Access silently returns **no rows** for
  `POLYGON Z` text, so SSURGO soil came back empty on the first run. After the fix MFP went from 0
  to 14 soil records (Wrightland 55, NERREC 53).
- **Commit:** `8699d68`

### 3. Multi-epoch state lidar + land-leveling detection — new, opt-in
- **What:** `ingest/download_state_lidar.py` clips every lidar DEM collection covering each field
  from ArcGIS ImageServers (default: Arkansas GIS Office 2026 0.5 m and 2011–2014 county 1 m),
  one locked collection per clip, raw float32, manifest with sha256; empty footprints recorded.
  `reporting/compare_lidar_epochs.py` aligns epochs, removes a farm-wide vertical bias, and reports
  dZ rasters, cut/fill, interior roughness and grade, edge-change and water/void flags, rule-based
  verdicts, per-field 4-panel figures, a farm CSV, and Markdown.
- **Why:** to check whether fields were leveled between the 2011 and 2026 flights.
- **Refinement (`76719f1`):** after reviewing real results, roughness/grade moved to a 10 m interior
  inset (new levees hid leveling), added "likely leveled (zero grade)" and "field-wide earthwork",
  "uniform shift (check data)", and spike flags relative to each field's expected spread.
- **Docs/tests:** READMEs, coverage tables, `tests/smoke_compare_lidar_epochs.py`.
- **Commits:** `3dc8549`, `76719f1`

### 4. Arkansas 0.5 m lidar as the main terrain source — new
- **What:** `terrain/dem-terrain/src/dem_terrain/arkansas_agio.py` adapter (`arkansas_agio_dem`),
  registered in `source_resolver.py`; `download_dem_terrain.py` and `run_farm_pipeline.py` gain the
  `arkansas` source policy. For Arkansas fields it outranks USGS 3DEP 1 m by resolution; USGS is the
  automatic fallback. All 35 fields now have every terrain derivative at 0.5 m.
- **Docs/tests:** `dem-terrain` PROVENANCE/README/SKILL (unpublished vertical datum caveat),
  `tests/smoke_arkansas_dem_adapter.py`.
- **Commit:** `2fd3584`

### 5. Per-field aerial orthoimagery clipper — new, opt-in
- **What:** `ingest/download_state_orthoimagery.py` clips 20 cm 2023 ADOP leaf-off RGB per field
  (true-metre CRS, ≤2048 px blocks, JPEG, manifest + preview). Shared ArcGIS export helpers moved to
  `lib/arcgis_imageserver.py`, which `download_state_lidar.py` now uses.
- **Why:** high-resolution context for soil patterns, levees, and checking lidar verdicts (e.g. it
  showed MFP field 227 is a contour-levee rice field).
- **Commit:** `7a26fcd`

### 6. Grower web-map subskill (Phase 3) — new
- **What:** `data-pipeline/grower-web-map/` (`SKILL.md`, `README.md`, `AGENTS.md`) and
  `reporting/generate_grower_web_map.py`: one lightweight, deterministic, self-contained Leaflet
  HTML map per grower from pipeline outputs — real field polygons, satellite basemap, click popups
  (grower, farm, field, acres, county, plus soil, crop history, NDVI, terrain, lidar change when
  present), farm-grouped field list that zooms to fields, filter, fit-all, and a growers index.
  Written from scratch (does not reuse `admin/interactive-web-map`).
- **Seeded and tested:** installed into `~/my-farm-advisor-runtime` with `install.sh`; generated for
  all three growers (18–31 KB each); verified in a browser.
- **Docs/tests:** routing in `my-farm-advisor/SKILL.md` and data-pipeline docs,
  `tests/smoke_grower_web_map.py`.
- **Commit:** `6a85062`

### 7. Web map: street basemap swapped to Esri World Street Map — change
- **What:** the "Streets" layer now uses Esri World Street Map
  (`server.arcgisonline.com/.../World_Street_Map`) instead of OpenStreetMap tiles; layer renamed
  "Streets (Esri)", attribution and footer updated, README/AGENTS updated, and the smoke test fails
  if OSM tile URLs return.
- **Why:** reviewing the downloaded map in Chrome, every OSM tile showed **403 "Access blocked"**:
  OSM's tile policy rejects pages opened from disk (`file://`, no Referer). Esri tiles load from
  local files (verified: all street tiles HTTP 200 on all three maps).
- **Commit:** this change set (see `git log`)

### 8. This change log — new
- `ASSIGNMENT-1-CHANGES.md`.

## Problems hit and how they were solved

| Problem | Fix |
|---|---|
| Coder's GitHub app could not create the fork ("Resource not accessible by integration") | Forked in the browser; fork was already public |
| Push to the fork returned 403 | Installed the Coder GitHub App on the fork repository |
| `fiona` would not build on Python 3.14 | Installed `libgdal-dev` and `python3.14-dev`, set `GDAL_CONFIG` |
| SSURGO soil empty | PolygonZ boundaries → importer writes 2D (change 2) |
| OpenCode lost the skills after a model-config sync rewrote `opencode.json` | Skills linked from `~/.config/opencode/skills/` to the worktree |
| State 2014 lidar footprints listed but empty over the fields | Recorded as `empty`; comparisons use 2011 vs 2026 |
| Lidar verdicts confused by datum offset, flight seams, new levees, water | Farm-wide bias, interior inset, uniform-shift and spike flags (change 3) |
| AGIO imagery server HTTP 500 on large reprojected exports | Tiled requests in 2048 px blocks (change 5) |
| OSM street tiles 403 in the downloaded map | Esri World Street Map (change 7) |

## Not committed (runtime and environment work)

- **Runtime:** `~/my-farm-advisor-runtime` (outside Git): shared data (geoadmin, lower-48 county
  weather and GDD, corn RM / soybean MG 2021–2025, Arkansas CDL), the three growers' full default
  pipeline outputs (16/16 steps each), 0.5 m terrain, lidar comparisons, aerial clips, web maps.
- **Grower source data:** my Arkansas shapefile stays in `~/workspace/arkansas-web-map/`, never in Git.
- **Environment:** system packages `libgdal-dev`, `gdal-bin`, `python3.14-dev`; OpenCode skill links
  in `~/.config/opencode/skills/`; data root persisted in `~/.config/environment.d/60-my-farm-advisor.conf`.
- **Kept upstream defaults:** CDL crop-rotation tables cover 2019–2023 (`DEFAULT_CDL_LATEST_YEAR = 2023`).

## Reproduce

```bash
export DATA_PIPELINE_DATA_ROOT=$HOME/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh --prepare-shared-data --cdl-scope state --cdl-state-fips 05
cd "$DATA_PIPELINE_DATA_ROOT/data-pipeline/src"; PY=../.venv/bin/python
$PY scripts/ingest/import_boundaries.py --source "/path/to/3 Farm Boundaries.shp" --state AR --state-fips 05
$PY scripts/run_farm_pipeline.py --grower-slug mfp --farm-slug farm-1723 --farm-name "1723"   # per farm
$PY scripts/ingest/download_state_lidar.py --grower mfp --farm farm-1723                       # optional
$PY scripts/reporting/compare_lidar_epochs.py --grower mfp --farm farm-1723                    # optional
$PY scripts/ingest/download_state_orthoimagery.py --grower mfp --farm farm-1723                # optional
$PY scripts/reporting/generate_grower_web_map.py --all-growers
```

Validation: `./scripts/validate.sh` (repo root) and the five offline smoke tests in
`my-farm-advisor/data-pipeline/src/scripts/tests/`.

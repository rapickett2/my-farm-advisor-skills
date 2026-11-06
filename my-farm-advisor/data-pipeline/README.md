# Data Pipeline Runtime Setup

Real-farm field polygons per country (CSB GeoParquet pruning, Overpass rules,
repository contract, new-country checklist):
[references/field-boundary-sources.md](references/field-boundary-sources.md).
Soil provider selection by boundary `country`:
[../soil/soil-routing.md](../soil/soil-routing.md).
Per-year crop rotation/crop-type sources by country (CDL, AAFC ACI, and the
keyless WorldCereal default-model path for every other country):
[references/crop-rotation-sources.md](references/crop-rotation-sources.md).
## Example renders

Vision-QC'd sample figures ship under [`examples/`](examples/) — one
subfolder per output family (`field-maps/`, `soil-maps/`, `maturity/`,
`field-components/`, `weather-eda/`, `worldcereal/`). PNGs only; regenerated
runtime artifacts stay outside the repo under `DATA_PIPELINE_DATA_ROOT`
(see `ASSET_POLICY.md` at the repo root).


## Global parity in one paragraph

Any field, any country, **zero accounts**: boundaries from CSB (US) or
OSM/Overpass (rules per country); profile soil from SSURGO (US), the Ontario
detailed survey (CA), Australia's Soil Atlas via Gaia-DAP (AU), or SoilGrids
250 m as the global floor; rotation history from CDL (US), AAFC ACI (CA), or
the WorldCereal default crop models applied per year on keyless Microsoft
Planetary Computer Sentinel-2 (`scripts/ingest/apply_worldcereal.py`,
verification figures via `scripts/render_worldcereal.py`, plus a USDA CDL
comparator panel via `scripts/render_worldcereal_cdl.py` — in-field class
shares and an explicit agree/disagree verdict); terrain from the
resolution-ranked DEM resolver (USGS 3DEP/TNM where mapped — up to 1 m — with
Copernicus 30 m, NASADEM, and ALOS as the global floor); weather from NASA
POWER public Zarr; admin boundaries and choropleths from geoadmin plus GADM
for any country via `--gadm-iso`. Every provider journals its
URL, sha256, and version. The one optional upgrade (Sentinel-1 fidelity in
WorldCereal runs, `--backend cdse`) is genuinely optional — the default path
never asks you to sign up for anything.

Per-field composite figures (terrain hillshade + contours, soil polygons, and
per-year crop class over the boundary) render from the same archive with
`scripts/render_field_maps.py`.

This subskill ships the scripts that build the data-pipeline reports and
posters. Each runtime host creates its own virtualenv inside the data tree on
first run; the scripts auto-bootstrap that environment before continuing.

## Quick start

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/bootstrap_farm_from_county.py \
  --state-fips 17 \
  --county-name DeKalb \
  --count 5 \
  --seed 77 \
  --grower-slug il-dekalb-grower \
  --farm-slug dekalb-demo-farm \
  --farm-name "DeKalb Demo Farm" \
  --run-pipeline \
  --force
```

For a first run that also initializes shared data and seeds fields for a grower in a state, use the installer directly from the checkout:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh \
  --prepare-shared-data \
  --seed-grower-slug acme-grower \
  --seed-state Illinois \
  --seed-field-count 12 \
  --seed-farm-name "Acme Illinois Farm"
```

That command installs the runtime source and venv, builds shared geoadmin L0/L1/L2 payloads, shared NASA POWER county weather, GDD, annual corn RM, annual soybean MG, five-year FIPS-average corn RM and soybean MG datasets, and last-five-year CONUS CDL rasters. It then selects a top-crop county in the requested state, samples the requested number of OSM fields, and runs the full farm pipeline so derived tables, field weather, real DEM terrain, soil outputs, CDL history, satellite/NDVI products, reports, cards, posters, and HTML/Markdown farm reports are generated automatically. The generated farm and field reports do not integrate terrain metrics in this change.

If the runtime is already installed, run the equivalent from the runtime source copy:

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/farm_dashboard.py create \
  --prepare-shared-data \
  --grower-slug acme-grower \
  --state Illinois \
  --field-count 12 \
  --farm-name "Acme Illinois Farm"
```

## Import your own field boundaries

To run the pipeline on grower-supplied polygons instead of sampled OSM/CSB fields, import any shapefile, GeoPackage, or GeoJSON with one row per field. Each grower/farm pair in the file becomes a canonical farm (boundary GeoJSON, `field-inventory.csv`, and `user_upload_fields_provenance.json` with the source sha256):

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/import_boundaries.py \
  --source "/path/to/Farm Boundaries.shp" \
  --state AR --state-fips 05 \
  --dry-run
```

Column names default to the common FMIS export layout (`CLIENT_NAM`, `FARM_NAME`, `FIELD_NAME`, `FIELD_ID`); override them with `--grower-column`, `--farm-column`, `--field-name-column`, and `--field-id-column`. Geometries are reprojected to EPSG:4326, repaired, and merged per field; `area_acres` is computed in EPSG:5070, and `county_fips` is filled from the shared county layer when present. Blank field IDs are derived as `<farm-slug>-<field-name-slug>`, and output is sorted, so re-importing the same file is byte-identical. `--state-fips` is required for US imports so the CDL step reads the right state. Drop `--dry-run` to write files, add `--grower "Name"` to import one grower, `--force` to replace boundaries that differ, and `--run-pipeline` to run `run_farm_pipeline.py` for each imported farm. Offline check: `scripts/tests/smoke_import_boundaries.py`.

Keep grower boundary files out of Git; only the runtime tree should hold them.

Boundaries are always written 2D: FMIS exports are often PolygonZ, and USDA Soil Data Access silently returns no rows for `POLYGON Z` WKT.

## Multi-epoch lidar and land-leveling detection (opt-in)

Where a state publishes several lidar flights as ArcGIS ImageServers, clip every collection that covers each field, then compare the flights. Defaults target the Arkansas GIS Office (free public services): `DEM_HALF_METER_2026` (0.5 m statewide, epoch 2026) and `DEM_1M_2018` (a mosaic of 2011–2014 county collections; each export is locked to one named collection, and the epoch is parsed from its name). Other states: pass `--service <ImageServer URL>` (repeatable).

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
PY="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"
"$PY" scripts/ingest/download_state_lidar.py --grower mfp --farm farm-1723 --dry-run
"$PY" scripts/ingest/download_state_lidar.py --grower mfp --farm farm-1723
"$PY" scripts/reporting/compare_lidar_epochs.py --grower mfp --farm farm-1723
```

`download_state_lidar.py` writes raw float32 clips (native CRS, snapped to each collection's pixel grid, field + 20 m buffer, split into strips under the 4100 px export limit) to `fields/<field>/terrain/state_lidar/<epoch>_<collection>.tif`, plus a manifest with service URL, collection, field coverage, sha256, and attribution. Collections whose footprint touches a field but hold no pixels there are recorded as `empty`. Re-runs skip clips whose manifest hash still matches; `--force` re-exports.

`compare_lidar_epochs.py` picks the best collection per epoch and, for every epoch pair, aligns both surfaces on the coarser grid (finer one cell-averaged) and computes dZ = later − earlier. One vertical bias per farm and collection pair (median dZ over the rings just outside all fields) is removed, because datum/geoid and processing offsets are farm-wide while earthwork is local; each field's own edge change is reported separately. It writes per-field dZ GeoTIFFs, JSON stats, a 4-panel PNG (both hillshades, dZ map with cut red / fill blue, elevation histograms), `derived/tables/<farm>_lidar_change_summary.csv`, and `derived/reports/<farm>_lidar_change.md`. Verdicts are rule-based with thresholds printed in every output:

| Verdict | Rule |
|---|---|
| likely leveled | interior plane-residual roughness drops to ≤ 0.6× and ≥ 20 % of the field changes by more than 0.15 m |
| likely leveled (zero grade) | ≥ 20 % changed and the later interior is ≤ 0.025 m rough on a ≤ 0.02 % plane (already-smooth fields re-leveled flat, typical of rice) |
| uniform shift (check data) | field and its edge moved together (> 0.10 m) without smoothing — a flight-line/calibration offset, not earthwork |
| field-wide earthwork (re-graded?) | ≥ 20 % changed otherwise (check the plane-slope change) |
| localized work | ≥ 5 % changed or roughness ≤ 0.8× |
| no significant change | otherwise |

Roughness and grade are measured on the field interior (10 m inset) so rebuilt perimeter levees and ditches do not hide leveling. Roughness ratios are ignored when the earlier surface was already smoother than 0.05 m: older 1 m lidar alone reads ~0.03 m on unchanged fields versus ~0.013 m for cell-averaged 0.5 m lidar. Flags call out field-edge (ditch/road/levee) change, large farm bias, and elevation spikes far above what the field's spread predicts (water surface or void fill). Inter-flight artifacts of 0.05–0.2 m are common: straight N–S/E–W edges in the dZ panel are data seams; curved, field-shaped patterns are earthwork. Offline checks: `scripts/tests/smoke_compare_lidar_epochs.py`.

`DATA_PIPELINE_DATA_ROOT` is required. Set it to an absolute writable path outside the skill checkout before running the installer or any pipeline entrypoint. There is no implicit fallback to a platform workspace path or to a checkout-local `data/` directory.

The installer creates and refreshes the runtime tree under:

- runtime base: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`
- runtime source copy: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src`
- default runtime venv: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv`

Generated outputs, manifests, reports, logs, and downloaded payloads belong under the runtime base, for example `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers` and `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared`. The committed checkout remains the source for installer scripts and baseline `src/` files, but runtime execution happens from the copied source.

Farm weather now uses NASA POWER's public S3 Zarr stores by default at actual field centroids. The default farm weather controls are `--weather-backend zarr`, `--weather-start-year 2021`, `--weather-end-year 2025`, and `--weather-time-standard lst`. The output path and CSV schema stay compatible with existing reports:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/derived/tables/<farm>_weather_2021_2025.csv
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/fields/<field>/weather/daily_weather.csv
```

Run or override those defaults from the runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py \
  --grower-slug il-dekalb-grower \
  --farm-slug dekalb-demo-farm \
  --farm-name "DeKalb Demo Farm" \
  --weather-backend zarr \
  --weather-start-year 2021 \
  --weather-end-year 2025 \
  --weather-time-standard lst
```

Use `--weather-backend api` only when explicitly debugging the legacy NASA POWER point API path for small field sets.

Shared county weather for maturity-by-FIPS uses NASA POWER's public S3 Zarr stores by default instead of issuing one `power.larc.nasa.gov` point API request per county grid cell. This avoids API rate-limit failures for L2 geoadmin scopes while preserving the existing output path and schema:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/weather/nasa-power/<year>/daily_weather_by_fips.parquet
```

For the full shared lower48 baseline, initialize the runtime with multi-year county weather, GDD, corn RM, soybean MG, corn/soybean five-year FIPS averages, and CDL raster outputs. The default shared maturity range is 2021-2025 to match the farm weather and CDL helper defaults; CDL initialization fetches the last five available CONUS rasters by default:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh --prepare-shared-data
```

That install flag runs the equivalent of:

```bash
python scripts/run_maturity_years_by_fips.py \
  --start-year 2021 \
  --end-year 2025 \
  --coverage lower48 \
  --weather-backend zarr \
  --weather-time-standard lst
python scripts/ingest/download_cdl.py \
  --raster-only \
  --cdl-scope conus \
  --cdl-latest-year 2025 \
  --cdl-window-years 5
```

`--prepare-shared-maturity` remains available for weather/GDD/corn/soy maturity only, but it does not prepare CDL rasters.

The maturity runner writes annual files like `shared/corn_maturity/tables/rm_by_fips_2025.parquet` and final five-year average files like `shared/corn_maturity/tables/rm_by_fips_2021_2025_average.parquet` and `shared/soybean_maturity/tables/mg_by_fips_2021_2025_average.parquet`.

For a single annual refresh, run:

```bash
python scripts/run_maturity_by_fips.py \
  --year 2025 \
  --coverage lower48 \
  --weather-backend zarr \
  --weather-time-standard lst
```

Use `--weather-backend api` only when explicitly debugging the legacy NASA POWER point API path for county weather.

## Default DEM terrain package

Normal farm pipeline initialization and add-field runs that invoke `scripts/run_farm_pipeline.py` include real DEM terrain by default. The pipeline calls `scripts/ingest/download_dem_terrain.py` immediately after field boundary download, passes live real-source permission through `--allow-live-downloads`, and writes elevation, slope, aspect, hillshade, curvature, wetness, depression, relative elevation, and erosion-proxy products under the runtime field tree. Generated farm and field reports do not integrate terrain metrics in this change.

Use `--skip-dem-terrain` only as an explicit operator override when a run must omit DEM terrain. The `scripts/run_farm_pipeline.py --structure-test` path remains a no-DEM, no-download structure check and must not import DEM-only dependencies or contact live services.

The direct DEM CLI remains available from the runtime source copy for focused dry runs, package inspection, or operator-led terrain retries. Direct CLI dry-run mode plans field paths and sources only. It does not write rasters, download DEM tiles, or require live provider services.

Safe temp-root install and dry-run check. This creates the runtime venv before invoking `.venv/bin/python`:

```bash
tmp_root="$(mktemp -d)"
export DATA_PIPELINE_DATA_ROOT="$tmp_root"
cd my-farm-advisor/data-pipeline
./scripts/install.sh --non-interactive --force-refresh
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/download_dem_terrain.py \
  --grower il-dekalb-grower \
  --farm dekalb-demo-farm \
  --context-meters 20 \
  --dry-run
```

For a no-network full-package smoke, use offline fixtures only as a direct CLI test-only synthetic path. These packages are not real grower DEM evidence, are not valid farm validation, and are marked with `synthetic_fixture=true`, `synthetic://...` source URLs, and warnings in the manifest/source reference. Synthetic fixture mode is unreachable from default farm-pipeline orchestration because `run_farm_pipeline.py` does not expose fixture flags. The CLI refuses fixture writes unless the deliberately named `--allow-synthetic-fixtures` test override is present. Prefer a temporary or disposable runtime root for this smoke:

```bash
tmp_root="$(mktemp -d)"
export DATA_PIPELINE_DATA_ROOT="$tmp_root"
cd my-farm-advisor/data-pipeline
./scripts/install.sh --non-interactive --force-refresh
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/download_dem_terrain.py \
  --grower il-dekalb-grower \
  --farm dekalb-demo-farm \
  --context-meters 20 \
  --offline-fixtures \
  --allow-synthetic-fixtures \
  --limit-fields 1
```

Focused direct CLI runs for real grower DEM verification must use cached real source DEM rasters or live provider access. Live DEM provider discovery or downloads are disabled in direct CLI runs unless you opt in. Add `--allow-live-downloads` only when the operator expects network access, provider availability, and downloaded DEM cache writes under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/dem/`:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/download_dem_terrain.py \
  --grower il-dekalb-grower \
  --farm dekalb-demo-farm \
  --context-meters 20 \
  --allow-live-downloads
```

Default farm orchestration already invokes real DEM terrain with live real-source controls after field boundaries. Use `--dem-context-meters` and `--dem-source-policy` to tune that default path, or `--skip-dem-terrain` when the operator explicitly chooses to omit DEM terrain for a run.

To persist the default data root for future login sessions, write the user environment file and still export the variable in the current shell before running commands:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d"
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/60-my-farm-advisor.conf" <<'EOF'
DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
EOF
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
```

The `environment.d` file applies to future sessions only. It does not update an already-running shell.

## Running inside an agent CLI

When invoking the pipeline from an agent harness CLI (OpenClaw, Hermes, or plain shell), you can still
activate the environment explicitly, but the entrypoints will install and re-exec
themselves if the runtime venv is missing.

```bash
bash -lc 'export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime && \
  cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src" && \
  "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
    scripts/run_farm_pipeline.py --grower-slug ... --farm-slug ...'
```

This ensures every pipeline step (including geopandas/rasterio operations) uses
the shared environment that lives alongside the replicated scripts.

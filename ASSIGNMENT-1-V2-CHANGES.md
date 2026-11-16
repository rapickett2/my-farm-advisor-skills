# Assignment 1 V2 — change log

Branch `assignment-1-v2` on the public fork `rapickett2/my-farm-advisor-skills`, branched from
`assignment-1` (`b0c8cac`) and compared with `main`. `assignment-1` is unchanged and remains the record
of the original submission; its change log (`ASSIGNMENT-1-CHANGES.md`) is kept as-is. Planned in
OpenCode **Plan mode**, built in **Build mode**.

## Why V2 exists

The original Assignment 1 ran the pipeline for 35 fields across three Arkansas growers. While planning
Assignment 2 (field-level EDA across growers), the sample turned out to be unsuitable:

- **Boundaries were partly derived, not drawn.** Only the Wrightland fields came from my own
  boundaries; candidate Missouri and Mississippi fields had been taken from the USDA Crop Sequence
  Boundaries (CSB). CSB is built by segmenting the CDL crop mask, so it covers only where crops were
  classified: inside my drawn blocks it covered **85% (MO), 89% (MS), 89% (AR pivot area)**, leaving
  holes for farmsteads, waterways, tree lines, small parcels and pivot corners, with edges snapped to
  30 m pixels. Field acreage and shape comparisons across growers would not have been valid.
- **One state.** All three original growers were in Arkansas, which gives little cross-grower
  contrast in weather or cropping.
- **Pivots could not be found from geometry.** No CSB parcel in Lee (AR) or Pemiscot (MO) was circular
  (best circularity 0.67), and drawn field boundaries do not trace the irrigated circle (the confirmed
  pivot `CH 03` has circularity 0.62). Irrigation is therefore recorded as a grower-supplied attribute.

V2 re-runs the whole Assignment 1 workflow on **30 fields I drew myself, 10 per grower, in three
states**. The instructor-approved substitution (real Mid-South growers instead of the Illinois/Iowa
demo growers) carries over.

## Sample

| Grower | Farm | County, State | Fields | Acres | Pivots | Boundary source |
|---|---|---|---|---|---|---|
| `wrightland-brothers` | `wrightland` | Lee, AR | `LC 01`–`LC 10` | 911.0 | `LC 07`, `LC 08` | my KML |
| `steele-cooter-farms` | `holland` | Pemiscot, MO | `HO 01`–`HO 10` | 519.7 | `HO 06` | my KML |
| `coahoma` | `coahoma` | Coahoma, MS | `CH 01`–`CH 10` | 708.6 | `CH 03` | my KML |
| | | | **30** | **2,139.3** | **4** | |

Field sizes range 9.9–226.7 acres (median 51.5). No CSB geometry is used for any field. The previous
runtime growers (`asu-precision-ag`,
`mfp`, and the eight original Wrightland `WR` fields) were archived to
`~/my-farm-advisor-runtime/superseded/`, not deleted, so Assignment 1 stays reproducible.

## Results

Every grower ran the full 16-step pipeline (`run_farm_pipeline.py`); no step was skipped or failed.

| Check | LC (AR) | HO (MO) | CH (MS) |
|---|---|---|---|
| Fields with boundaries | 10 | 10 | 10 |
| SSURGO soil | 10 fields, 68 soil polygons (88 component rows) | 10 fields, 49 polygons (68 rows) | 10 fields, 60 polygons (57 rows) |
| SoilGrids | 9 bands × 3 depths, 270 rasters | same | same |
| NASA POWER weather | 1,826 daily rows/field (2021–2025) | same | same |
| CDL crop history | 10/10 fields, 2019–2023 | 10/10 | 10/10 |
| State lidar | 2026 0.5 m + 2011 1 m, 100% of every field | 2024 0.5 m, 100% | 2019 1 m, 100% |
| Land-leveling comparison | 2011 → 2026 | single epoch | single epoch |
| Web map / NDVI viewer | 103 KB, 68 polygons / 792 scenes | 91 KB, 49 / 688 | 111 KB, 60 / 782 |

**Arkansas land leveling (2011 → 2026):** `LC 03` and `LC 09` show *localized work* (10.9% of
`LC 09`'s surface changed, field grade 1.07%); the other eight show *no significant change*. No field
was flagged for seams, water or spikes. The 2014 county flight is listed over all ten LC fields but
holds no pixels inside any of them, so it is recorded as `empty` and the comparison uses 2011 vs 2026,
as in Assignment 1.

**Pivots:** four grower-confirmed center pivots (`LC 07` 182.6, `LC 08` 50.5, `HO 06` 156.3, `CH 03`
64.8 ac) cover 454.2 of 2,139.3 acres (21%). The other 26 fields are recorded as not center pivot.

## Changes on the branch (committed)

### 1. Boundary importer reads KML and explodes multi-part placemarks — change
- **What:** `ingest/import_boundaries.py` gains `--grower-name` / `--farm-name` (constants for sources
  without grower/farm columns), `--explode-multipart` (one field per polygon part) and
  `--multipart-prefix` (parts numbered `<prefix> 01…`, ordered north-to-south then west-to-east so
  re-imports keep the same numbers).
- **Why:** my farm-app KML exports have no attribute columns and pack each farm's ten fields into one
  placemark. Without exploding, each farm imported as a single 500–900 acre "field".
- **Still enforced:** geometry is written 2D (USDA Soil Data Access returns no rows for `POLYGON Z`).
- **Docs/tests:** data-pipeline README and AGENTS; `tests/smoke_import_boundaries.py` adds a ten-part
  KML case (count, names, ordering, 2D).
- **Commit:** `4e8e195`

### 2. State lidar presets for Missouri and Mississippi — change
- **What:** `ingest/download_state_lidar.py` picks a service preset from the farm's state
  (`--preset` overrides):

  | Preset | Service | Resolution | Epoch |
  |---|---|---|---|
  | `arkansas` | Arkansas GIS Office `DEM_HALF_METER_2026` + `DEM_1M_2018` county collections | 0.5 m / 1 m | 2026 + 2011–2014 |
  | `missouri` | MSDIS `MO_Southeast11Counties_2024_B24_DEM_UTM` (USGS 3DEP QL1) | 0.5 m | 2024 |
  | `mississippi` | MARIS/MDEQ `MS_Statewide_Lidar_Mosaic_2019_IS` | 1 m (requested) | 2019 |

  A new **mosaic mode** exports one clip per service per field: the MO and MS services index
  individual tiles rather than whole flights, so the Arkansas "one locked clip per collection" rule
  would have split one flight into tile fragments. The Mississippi mosaic is in US survey feet on Web
  Mercator; clips are requested in UTM 15N (true metres) and converted to metres, and the manifest
  records the conversion.
- **Why:** I asked for lidar in all three states, not only Arkansas. Verified on real fields before
  committing: HO 01 77.6 m mean (0.5 m, EPSG:6344), CH 03 51.5 m mean (matches the 168.7 ft service
  value).
- **Limit:** MO and MS each have **one** flight over these fields (the other MSDIS services return no
  data in Pemiscot), so they get high-resolution terrain but no land-leveling change verdicts.
- **Commit:** `218aefd` (docs `6eba732`)

### 3. Web map: irrigation row from `manifests/field-attributes.csv` — new
- **What:** an optional per-farm `field-attributes.csv` (`field_id,irrigation,irrigation_source`)
  adds an **Irrigation** popup row (Center pivot / Not center pivot / Furrow / flood / Dryland). Fields
  without a value get no row.
- **Why:** pivot status is a fact I supplied, not something the boundaries reveal (see "Why V2 exists").
- **Docs/tests:** grower-web-map README; smoke test checks the row appears only from the file.
- **Commit:** `489701c`

### 4. Fix: resumable CDL download that never caches a truncated raster — bug fix
- **What:** `ingest/download_cdl.py` downloads to `<name>.part`, resumes with HTTP Range when the
  connection drops, checks the size against `Content-Length`, and renames only when complete.
- **Why:** the NASS `byfips` server closed **every one** of the 14 Missouri and Mississippi state
  raster transfers early (each needed 2–4 resumes). The old code streamed straight to the cached
  path, so a dropped transfer left a truncated `.tif` that every later run would treat as cached.
- **Commit:** `14fb6b2`

### 5. Fix: full USDA CDL legend in `cdl_reporting` — bug fix
- **What:** `soil/cdl-cropland/src/cdl_reporting.py` `CDL_CODES` replaced with the full USDA NASS
  legend (135 classes). The old table had 11 Iowa-oriented codes and mislabelled four (28 is Oats,
  not Alfalfa; 36 Alfalfa, not Forest; 38 Camelina, not Grassland; 43 Potatoes, not Open Water).
- **Why:** codes missing from the table became `Code_<n>` and `filter_cdl_categories` dropped them, so
  **cotton (2), rice (3), peanuts (10) and double-crop wheat/soybeans (26) vanished**. The crop history
  then fell back to whatever minor class remained, so it was wrong, not just incomplete: LC 04 read
  "Soybeans ×5" but is Cotton → Peanuts → Cotton → Cotton → Peanuts. Found in Assignment 2 when LC 09
  lost its 2019 and 2021 cotton years. Arkansas CDL tables, NDVI composites/cards, posters, and farm
  reports were regenerated after the fix; Missouri and Mississippi ran with it from the start.
- **Also found:** the runtime's source locator (`.my-farm-advisor-source.json`) still pointed at the
  original Assignment 1 worktree; it now points at this checkout so the fix is what actually runs.
- **Commit:** `8c50546`

### 6. Web map: state lidar popup row — new
- **What:** a **State lidar** popup row lists the state lidar clips actually on disk
  (`fields/<field>/terrain/state_lidar/manifest.json`), newest epoch first with its resolution, and
  skips clips recorded as `empty`.
- **Why:** the Terrain source row reports the pipeline's DEM, which for Missouri is the 2014 3DEP
  flight (1 m). The 2024 USGS QL1 clip (0.5 m) downloaded by `download_state_lidar.py` was invisible in
  the map, so the V2 lidar work looked absent.
- **Docs/tests:** grower-web-map README; smoke test asserts ordering, resolution formatting and the
  empty-clip skip.
- **Commit:** `c0be62b`

### 7. Fix: share USGS 3DEP tiles between fields and stream downloads — bug fix
- **What:** `terrain/dem-terrain/src/dem_terrain/usgs_tnm.py` reuses an identical tile already cached
  for a sibling field (hard link, or copy across filesystems) and streams downloads to disk in 1 MB
  chunks with a `Content-Length` check, instead of reading the whole raster into memory.
- **Why:** the DEM cache is keyed by field, but a 3DEP project tile covers many fields. One Mississippi
  tile is **331 MB** and took ~100 min to fetch; all ten CH fields downloaded the same bytes again, so
  the CH run was killed after 13.9 h. The three remaining fields reused the tile already on disk.
- **Docs/tests:** new offline `tests/smoke_usgs_tnm_cache.py` covers sibling reuse, streaming and a
  failed transfer; the two existing DEM smoke tests still pass.
- **Commit:** `0f13896`

## Problems hit and how they were solved

| Problem | Fix |
|---|---|
| CSB-derived fields had 11–15% gaps inside every drawn block | Drew all 30 boundaries myself (see "Why V2 exists") |
| A stale pivot-footprint KML pointed at `LC 04` + `LC 07`; the real pivots are `LC 07` + `LC 08` | Footprint discarded; pivots taken only from the grower |
| Two review KMLs had been written in EPSG:5070 metres and opened in "outer space" in Google Earth | KML always written from EPSG:4326 with a lon/lat range assertion |
| KML placemark = whole farm | `--explode-multipart` (change 1) |
| MO/MS lidar services are tiled, MS is in feet on Web Mercator | Mosaic mode, UTM 15N request, feet→m (change 2) |
| SoilGrids clips at ~90 s per raster (≈40 min per field) | Pre-clipped MO and MS SoilGrids in parallel band groups; their pipeline runs reuse the clips |
| ISRIC SoilGrids server returned errors under six parallel workers (4 of 6 failed) | Re-ran with two workers and GDAL HTTP retries; the idempotent script filled the 145 missing rasters |
| NASS CDL server dropped every large transfer | Resumable download (change 4) |
| Cotton, peanuts, rice missing from crop histories | Full CDL legend (change 5) |
| Mississippi 3DEP elevation ran ~105 min/field (Missouri: 1.6 min/field) | Root cause: the per-field cache re-downloaded one 331 MB tile per field, so six parallel shard workers duplicated the whole queue. Fixed in the adapter (change 7); the three missing fields linked the tile already on disk |

## Not committed (runtime and environment work)

- **Runtime:** `~/my-farm-advisor-runtime` (outside Git): the three V2 growers' full default pipeline
  outputs, state lidar clips, Arkansas lidar comparison and aerial clips, `field-attributes.csv`, web
  maps and NDVI viewers. Superseded growers in `~/my-farm-advisor-runtime/superseded/`.
- **Grower source data:** my three KML exports stay outside Git.

## Reproduce

```bash
export DATA_PIPELINE_DATA_ROOT=$HOME/my-farm-advisor-runtime
cd "$DATA_PIPELINE_DATA_ROOT/data-pipeline/src"; PY=../.venv/bin/python
$PY scripts/ingest/import_boundaries.py --source "LC Boundaries.kml" --field-name-column Name \
  --grower-name "Wrightland Brothers" --farm-name Wrightland --explode-multipart --multipart-prefix LC \
  --state AR --state-fips 05
$PY scripts/ingest/import_boundaries.py --source "HO Boundaries.kml" --field-name-column Name \
  --grower-name "Steele Cooter Farms" --farm-name Holland --explode-multipart --multipart-prefix HO \
  --state MO --state-fips 29
$PY scripts/ingest/import_boundaries.py --source "CH Boundaries.kml" --field-name-column Name \
  --grower-name Coahoma --farm-name Coahoma --explode-multipart --multipart-prefix CH \
  --state MS --state-fips 28
$PY scripts/run_farm_pipeline.py --grower-slug wrightland-brothers --farm-slug wrightland --farm-name Wrightland \
  --weather-backend zarr --weather-start-year 2021 --weather-end-year 2025 --weather-time-standard lst
$PY scripts/run_farm_pipeline.py --grower-slug steele-cooter-farms --farm-slug holland --farm-name Holland \
  --weather-backend zarr --weather-start-year 2021 --weather-end-year 2025 --weather-time-standard lst
$PY scripts/run_farm_pipeline.py --grower-slug coahoma --farm-slug coahoma --farm-name Coahoma \
  --weather-backend zarr --weather-start-year 2021 --weather-end-year 2025 --weather-time-standard lst
# hand-written per farm: growers/<grower>/farms/<farm>/manifests/field-attributes.csv
#   field_id,irrigation,irrigation_source   (pivot: LC 07, LC 08, HO 06, CH 03; others non_pivot)
$PY scripts/ingest/download_state_lidar.py --grower <grower> --farm <farm>            # all three
$PY scripts/reporting/compare_lidar_epochs.py --grower wrightland-brothers --farm wrightland
$PY scripts/ingest/download_state_orthoimagery.py --grower wrightland-brothers --farm wrightland
$PY scripts/reporting/generate_grower_web_map.py --all-growers
```

Validation: `./scripts/validate.sh` (repo root) and the offline smoke tests in
`my-farm-advisor/data-pipeline/src/scripts/tests/`.

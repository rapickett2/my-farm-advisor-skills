---
name: field-eda
description: Field-level exploratory analysis and cross-grower comparison of data-pipeline outputs — field boundaries, NASA POWER weather, and USDA CDL cropland — with static figures, a geospatial map, and a one-page HTML report.
version: 1.0.0
author: rapickett2
tags: [eda, field-level, comparison, boundaries, weather, cdl, static-report]
---

# Workflow: field-eda

## Description

Compare fields and grower units using only what the data pipeline already produced. Three
categories, each with two field-level views and one grower comparison, plus one map:

| Category | View 1 | View 2 | Grower comparison |
|---|---|---|---|
| Field boundaries | Acreage of every field, pivots hatched | Boundary length per acre vs size, against the square-field minimum | Size, rectangularity, edge per acre by grower + Kruskal–Wallis |
| Weather (NASA POWER) | Monthly rainfall and Tmax climatology | Cumulative Apr–Sep GDD (86/50 °F) per year | Season GDD, rainfall, days ≥ 95 °F by year |
| Cropland (USDA CDL) | Majority crop per field per year (rotation grid) | Majority-crop share vs field size | Acre-weighted crop mix by year + rotation rate |
| Map | Each grower's fields on imagery coloured by the latest crop, pivots dashed, locator panel | | |

Output is static only: PNG figures, CSV tables, and one self-contained HTML page (images embedded,
no scripts). Findings in the report are computed from the tables, not typed in.

## When to use

- Comparing growers, farms, or regions at field level after a pipeline run
- Checking whether field size, shape, weather, or crop history differ between operations
- Producing a short, reproducible EDA report for review

Out of scope by design: soil (see `../../soil/`), interactive maps or dashboards (see
`../../data-pipeline/grower-web-map/`).

## Prerequisites

- A completed data-pipeline run for each farm (boundaries, weather, inventory), see
  `../../data-pipeline/README.md`. Field boundaries should be grower-drawn where possible.
- CDL state rasters under `shared/cdl/rasters/CDL_<year>_<state_fips>.tif` for every year requested.
  The pipeline downloads 2019–2023 by default; fetch later years from
  `https://nassgeodata.gmu.edu/nass_data_cache/byfips/CDL_<year>_<fips>.tif`.
- Optional `manifests/field-attributes.csv` per farm (`field_id,irrigation,irrigation_source`) for
  pivot status. Never infer pivots from boundary shape.
- The runtime venv (`geopandas`, `rasterio`, `rasterstats`, `scipy`, `matplotlib`, `contextily`).

## Quick start

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
PY="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"
"$PY" my-farm-advisor/eda/field-eda/src/run_field_eda.py \
  --unit wrightland-brothers:wrightland \
  --unit steele-cooter-farms:holland \
  --unit coahoma:coahoma \
  --cdl-years 2019-2025 --weather-years 2021-2025
```

Outputs go to `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/field-eda/`:

```
field_eda_report.html        one page, figures embedded
figures/01_…10_*.png         static figures
tables/*.csv                 fields, season weather, CDL composition and majority crop, summaries
```

Add `--publish-dir <dir>` to copy the report and figures somewhere committable, and
`--no-basemap` to draw the map without online imagery tiles.

## Method notes

- **Areas and shape** are computed in EPSG:5070. Rectangularity = area ÷ minimum rotated rectangle;
  edge per acre = perimeter ÷ acres, compared with a square of the same area.
- **Weather** is each field's pipeline `daily_weather.csv`. GDD use the corn 86/50 °F method, summed
  Apr 1–Sep 30. Fields in one farm usually share a NASA POWER cell; the report measures and states the
  within-farm spread so weather is read as a grower comparison.
- **CDL** composition uses the cdl-cropland `extract_crop_composition` routine (the same one the
  pipeline uses), then keeps the majority class per field-year. Rotation rate = acre-weighted share of
  field-years whose crop differs from the year before.
- **Tests** are Kruskal–Wallis across growers and Spearman for purity vs size; with ~10 fields per
  grower they are indicative only, and the report says so.

## Validation

```bash
"$PY" my-farm-advisor/eda/field-eda/src/smoke_field_eda.py   # offline, synthetic runtime
./scripts/validate.sh                                          # repo root
```

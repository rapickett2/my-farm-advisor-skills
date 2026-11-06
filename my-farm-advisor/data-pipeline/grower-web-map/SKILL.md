---
name: grower-web-map
description: >
  Generate one lightweight, self-contained interactive HTML web map per grower from data-pipeline
  outputs: real field boundary polygons on an online ESRI World Imagery satellite basemap, click
  popups with grower / farm / field metadata (plus soil, crop history, NDVI, terrain, and lidar
  change when present), a field list that zooms to each field, and a fit-all control.
---

# Grower Web Map

Use this subskill when the user wants an interactive map of a grower's fields after the data
pipeline has run, or wants to demo/share pipeline results in a browser.

## Run

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --all-growers      # or --grower <slug> (repeatable)
```

Outputs: `growers/<grower>/derived/reports/<grower>_web_map.html` and, with `--all-growers`,
`growers/web_maps_index.html`.

## Next

- [README.md](README.md) — what the map shows, popup data sources, size and determinism rules.
- [AGENTS.md](AGENTS.md) — local editing and validation rules.
- Script: `../src/scripts/reporting/generate_grower_web_map.py`; offline test:
  `../src/scripts/tests/smoke_grower_web_map.py`.

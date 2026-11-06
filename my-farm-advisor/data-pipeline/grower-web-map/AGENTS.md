# Grower Web Map Local Instructions

## Scope

This subskill owns `../src/scripts/reporting/generate_grower_web_map.py`, its smoke test
`../src/scripts/tests/smoke_grower_web_map.py`, and the docs in this folder. Keep edits here unless
the user asks for broader data-pipeline changes.

## Rules

- Read only data-pipeline runtime outputs under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers`.
  Never read grower source files (shapefiles, exports) directly; import them with
  `ingest/import_boundaries.py` first.
- Optional popup sources must stay optional: a missing file skips its rows, never fails the map.
- Keep output lightweight: polygons, short text, and the per-field average-NDVI-by-crop maps only.
  Those NDVI maps are the single allowed embedded image type: field-clipped, native 10 m,
  colourised PNG data URLs. Do not embed raw imagery, aerial photos, terrain or other rasters, or
  full tables. Keep the size warning (1 MB), the 500 KB smoke-test budget, and the 0.5 m
  simplification.
- NDVI averages use only CDL-labelled years; never fold "Unknown" years into a crop average.
- Keep output deterministic: sorted fields, `sort_keys` JSON, ordered popup rows as `[label, value]`
  pairs, no timestamps.
- Basemaps must work from a downloaded `file://` page: keep to Esri ArcGIS Online tile services
  (World Imagery, World Street Map, reference labels). Do not use `tile.openstreetmap.org`; it
  answers file:// pages with HTTP 403 "Access blocked". The smoke test enforces this.
- Keep colours accessible: farm colours stay in the Okabe–Ito palette, outlines keep the dark
  casing for contrast on both basemaps, and state (selection) is never shown by hue alone.
- Escape every string rendered into the page (HTML-escape server side, `esc()` client side).
- Generated HTML is runtime output; never commit it.

## Validation

After changes, run the smoke test with the runtime venv Python, regenerate maps with
`--all-growers`, open one in a browser (or a headless browser) to confirm tiles, polygons, popup,
and field-list zoom, then run `./scripts/validate.sh` from the repo root.

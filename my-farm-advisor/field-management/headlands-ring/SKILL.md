---
name: headlands-ring
description: >
  Build a reusable headlands ring for one field boundary: read the boundary CRS first, measure in a
  UTM metric workspace, subtract a negative inner buffer (default 21 m), and write
  field_boundary.gpkg and headlands_ring.gpkg in EPSG:4326 with meters_squared and acres.
---

# Headlands Ring

Use this subskill when the user wants the headlands (turn-row / edge strip) of a field as a map
layer with its area, or wants field and headlands acres for one field.

## Run

```bash
PY="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python"
FIELD="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/fields/<field>"
"$PY" my-farm-advisor/field-management/headlands-ring/scripts/make_headlands_ring.py \
  --input "$FIELD/boundary/field_boundary.geojson" \
  --out-dir "$FIELD/derived/features/headlands_ring_21m" \
  --width-m 21
```

Outputs (EPSG:4326, one row each, columns `geometry`, `meters_squared`, `acres`):
`field_boundary.gpkg` and `headlands_ring.gpkg`. Existing outputs are never overwritten unless
`--force` is passed.

Add `--pictures --title "WR 02"` to the same command to also write the overview map and six-panel
workflow figure. To draw them later from existing files:

```bash
"$PY" my-farm-advisor/field-management/headlands-ring/scripts/make_headlands_pictures.py \
  --dir "$FIELD/derived/features/headlands_ring_21m" --title "WR 02"
```

## Rules

- Inspect the input CRS before reprojecting; a missing CRS is an error, never assumed.
- Measure areas and buffer only in the UTM workspace; convert only the ring back to EPSG:4326.
- Keep generated `.gpkg` files under the runtime data root, not in Git.

## Next

- [README.md](README.md) for the step-by-step workflow and NERREC 17 example.
- [GUIDE.md](GUIDE.md) for the Python helpers (points, polygons, plots).
- Tests: `scripts/tests/smoke_make_headlands_ring.py`, `scripts/tests/smoke_make_headlands_pictures.py`.

# Headlands Ring

Create the headlands ring (the strip along the inside edge of a field where equipment turns) for
one field boundary, and save both the field and the ring as GeoPackages with their areas.

## Workflow

| Step | Space | Action |
|---|---|---|
| 1 | EPSG:4326 | Read the boundary as `original_gdf`. The CRS is inspected first: CRS84 / EPSG:4326 is used as-is, another CRS is converted to EPSG:4326 once (logged), a missing CRS stops the run. |
| 2 | UTM | Copy `original_gdf` to a UTM working GeoDataFrame (zone estimated from the field). |
| 3 | UTM | Calculate the full boundary area: `meters_squared` and `acres`. |
| 4 | UTM → 4326 | Write both area attributes back to `original_gdf` (geometry stays EPSG:4326). |
| 5 | UTM | Create a negative inner buffer (`--width-m`, default 21 m). |
| 6 | UTM | Difference: full boundary − inner buffer = headlands ring. If the field is too narrow, the whole field is the ring. |
| 7 | UTM | Calculate the headlands ring area: `meters_squared` and `acres`. |
| 8 | UTM → 4326 | Convert only the headlands ring back to EPSG:4326. |
| 9 | EPSG:4326 | Write `field_boundary.gpkg` and `headlands_ring.gpkg`. |

Both outputs have one row and the columns `geometry`, `meters_squared`, `acres`.
Acres = m² × 0.000247105.

## Usage

```bash
python scripts/make_headlands_ring.py --input <boundary file> --out-dir <folder> [--width-m 21] [--pictures] [--force]
```

- `--input`: any one-field vector file GeoPandas can read (GeoJSON, GeoPackage, shapefile).
- `--out-dir`: a new folder; the script refuses to overwrite existing outputs without `--force`.
- The input boundary file is only read, never modified.

## Pictures

One command for files and pictures: add `--pictures [--title "WR 02"] [--prefix wr02]` to
`make_headlands_ring.py`. To draw pictures from existing GeoPackages:

```bash
python scripts/make_headlands_pictures.py --dir <folder with the two .gpkg files> --title "WR 02" [--prefix wr02] [--dpi 170] [--force]
```

Writes next to the GeoPackages:

- `<prefix>_headlands_overview.png`: metric map with the field, yellow headlands ring, dashed inner
  buffer line, a callout, and ring / field / interior areas.
- `<prefix>_headlands_workflow_steps.png`: six panels following slide steps 1-9.

`--prefix` defaults to a slug of the title. The interior is the ring's hole, so no width argument is
needed. Existing pictures are never overwritten without `--force`.

## Example: NERREC 17 (ASU-Precision AG, Poinsett County, AR)

```bash
export DATA_PIPELINE_DATA_ROOT=$HOME/my-farm-advisor-runtime
PY=$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python
FIELD=$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers/asu-precision-ag/farms/nerrec/fields/nerrec-17
$PY my-farm-advisor/field-management/headlands-ring/scripts/make_headlands_ring.py \
  --input $FIELD/boundary/field_boundary.geojson \
  --out-dir $FIELD/derived/features/headlands_ring_21m
```

Result: input CRS84 / EPSG:4326 used without reprojection, UTM zone 15N (EPSG:32615) workspace,
field 129,311.81 m² = 31.95 ac, 21 m headlands ring 33,545.89 m² = 8.29 ac (25.9 % of the field).

## Tests

```bash
python scripts/tests/smoke_make_headlands_ring.py
python scripts/tests/smoke_make_headlands_pictures.py
```

Offline; uses a synthetic 200 m square (expected field 40,000 m², 21 m ring 15,036 m²) and checks
CRS, row count, columns, areas, CRS handling for EPSG:5070 and missing-CRS inputs, and the
no-overwrite rule. The pictures test builds a normal and a narrow (whole-field ring) synthetic
field, checks both PNGs, the no-overwrite rule, and rejection of non-EPSG:4326 inputs.

## Dependencies

`geopandas`, `shapely`, `pyproj`, `matplotlib` (imported by the shared helpers in `src/`). The
data-pipeline runtime virtual environment already has them.

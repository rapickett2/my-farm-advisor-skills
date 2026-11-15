# Field EDA

Field-level exploratory analysis and grower-to-grower comparison built only from data-pipeline
outputs. It answers three questions for a set of farms — how the fields differ in size and shape,
how the growing-season weather differs, and what each field has been planted to — and writes the
answers as static figures and a one-page report.

Start with [`GUIDE.md`](GUIDE.md) for the workflow and command; agents also read
[`AGENTS.md`](AGENTS.md).

## What it produces

| # | Figure | Category | Kind |
|---|---|---|---|
| 1 | Field size, every field, pivots hatched | Boundaries | field view |
| 2 | Boundary length per acre vs size, against a square field | Boundaries | field view |
| 3 | Size, rectangularity, edge per acre by grower + Kruskal–Wallis | Boundaries | grower comparison |
| 4 | Monthly rainfall and Tmax, 5-year mean | Weather | view |
| 5 | Cumulative Apr–Sep growing degree days by year | Weather | view |
| 6 | Season GDD, rainfall, days ≥ 95 °F by year and grower | Weather | grower comparison |
| 7 | Majority CDL crop per field per year | Cropland | field view |
| 8 | Majority-crop share vs field size | Cropland | field view |
| 9 | Crop mix by year and rotation rate per grower | Cropland | grower comparison |
| 10 | Fields on imagery coloured by latest crop, pivots dashed, locator | Geospatial | map |

Plus CSV tables (fields, season weather, CDL composition, summaries) and `field_eda_report.html`, a
single self-contained page with every figure embedded and findings computed from the tables.

## Committed report

[`report/field_eda_report.html`](report/field_eda_report.html) is the run for the three Mid-South
growers (30 grower-drawn fields in Lee Co. AR, Pemiscot Co. MO, and Coahoma Co. MS; CDL 2019–2025;
NASA POWER 2021–2025). The same figures are in [`report/figures/`](report/figures/) so they display
on GitHub. Download the HTML and open it in a browser to read the page.

## Scope

Static Python output only (matplotlib, geopandas, rasterstats, scipy). No soil — that is a separate
skill area — and no interactive maps or dashboards; for those use the grower web map in
`../../data-pipeline/grower-web-map/`.

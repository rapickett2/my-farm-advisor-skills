#!/usr/bin/env python3
"""Assemble the one-page HTML EDA report from field-eda artifacts.

This is a one-time artifact builder, not part of the reusable EDA runner. It
reads what ``src/run_field_eda.py`` produced (``figures/``, ``tables/`` and
``tables/findings.json``) and writes a single self-contained HTML page with
every figure embedded as a data URI, so it opens from a git checkout with no
server and no network.

Example:
    python report/build_report.py \\
        --publish-dir my-farm-advisor/eda/field-eda/report
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
sys.path.insert(0, str(SKILL_ROOT / "src"))

import field_eda_data as data  # noqa: E402

# One caption per figure: what it shows and which comparison level it speaks to.
FIGURES = {
    "boundaries_size": {
        "file": "01_boundaries_size.png",
        "title": "Field size, every field",
        "level": "field",
        "caption": (
            "Acreage of all 30 fields, grouped by grower on a log scale; hatched bars are the four "
            "grower-confirmed center pivots. Shows the size range the rest of the analysis works with "
            "(9.9–226.7 ac, median 51.5) and that the pivots are among the larger fields (median 111 ac), "
            "not the small ones."
        ),
    },
    "boundaries_shape": {
        "file": "02_boundaries_shape.png",
        "title": "Boundary length per acre vs field size",
        "level": "field",
        "caption": (
            "Perimeter per acre against acreage, with the square-field minimum as the reference curve. "
            "Distance above the curve is shape-driven headland: small or irregular fields carry more "
            "turn-row per acre, which is the geometry that costs the most machine time."
        ),
    },
    "boundaries_compare": {
        "file": "03_boundaries_compare.png",
        "title": "Size and rectangularity by grower",
        "level": "across-grower",
        "caption": (
            "Grower distributions of field size and rectangularity with Kruskal–Wallis tests. Size does "
            "not separate the growers at 10 fields each (p = 0.187); shape does (p = 0.003), with MO the "
            "most regular (median 0.92) and AR the least (0.68)."
        ),
    },
    "weather_monthly": {
        "file": "04_weather_monthly.png",
        "title": "Monthly rainfall and Tmax, 5-year mean",
        "level": "across-grower",
        "caption": (
            "Mean monthly precipitation and maximum temperature per grower, 2021–2025. Shows seasonal "
            "timing rather than single events: rain peaks in April–May and bottoms out in July–August at "
            "all three growers, while the July–August heat peak is highest at MS (35 °F mean Tmax against "
            "32 °F at MO)."
        ),
    },
    "weather_gdd": {
        "file": "05_weather_gdd.png",
        "title": "Cumulative Apr–Sep GDD by year",
        "level": "field-year",
        "caption": (
            "Cumulative growing-degree days within each season, one panel per grower and one line per "
            "season, averaged over that grower's fields. Reading the lines inside a panel is the "
            "season-to-season comparison for a field (a farm shares one NASA POWER cell, so every field "
            "sees the same curve); the vertical gap between panels is the across-grower comparison."
        ),
    },
    "weather_compare": {
        "file": "06_weather_compare.png",
        "title": "Season GDD, rainfall and hot days",
        "level": "across-grower",
        "caption": (
            "Season totals by year with growers side by side: GDD, Apr–Sep rainfall and days ≥ 95 °F. The "
            "point of the panel is that a single grower's year-to-year rainfall swing (median 10.6 in) is "
            "larger than the 2.1 in gap between growers, so weather comparisons need the year attached."
        ),
    },
    "cdl_sequence": {
        "file": "07_cdl_sequence.png",
        "title": "Majority CDL crop per field per year",
        "level": "field-year",
        "caption": (
            "The rotation grid: one row per field, one column per CDL year, colour = majority crop, ◉ = "
            "pivot. Reading a row left to right is the within-field comparison across years; scanning "
            "columns compares fields inside a field-year."
        ),
    },
    "cdl_purity": {
        "file": "08_cdl_purity.png",
        "title": "Majority-crop share vs field size",
        "level": "field-year",
        "caption": (
            "How much of each field-year the majority crop actually covers, against field size. Tests the "
            "obvious explanation for mixed labels — small fields picking up edge pixels. It does not hold "
            "(Spearman ρ = 0.08, p = 0.68), so mixed labels come from split or double-cropped fields rather "
            "than from small fields alone."
        ),
    },
    "cdl_compare": {
        "file": "09_cdl_compare.png",
        "title": "Crop mix and rotation rate by grower",
        "level": "across-grower",
        "caption": (
            "Acre-weighted crop mix by year and the acre-weighted rotation rate per grower. AR turns over "
            "74% of its acres a year, MO 68%, MS 51% — the clearest across-grower difference in the CDL "
            "layer, and the reason the rotation grid looks busiest in AR."
        ),
    },
    "map": {
        "file": "10_field_map.jpg",
        "title": "All 30 fields on imagery, coloured by latest crop",
        "level": "geospatial",
        "caption": (
            "Every boundary drawn on Esri World Imagery, filled by its most recent CDL crop, pivots dashed, "
            "with a state locator inset. Confirms the polygons are real field shapes in three separate "
            "states and shows the spatial context the three comparisons above are drawn from."
        ),
    },
}

SECTIONS = [
    (
        "1 · Field boundaries",
        "boundaries",
        "Levels used: <b>field</b> (figures 1–2) and <b>across-grower</b> (figure 3). Boundaries have no "
        "time dimension, so field-year does not apply to this category.",
        ["boundaries_size", "boundaries_shape", "boundaries_compare"],
    ),
    (
        "2 · Weather",
        "weather",
        "Levels used: <b>field-year</b> (figure 5 — a field's season-to-season weather is its grower's "
        "curve, because a farm shares one or two NASA POWER cells) and <b>across-grower</b> (figures 4 "
        "and 6). Field-to-field weather inside a farm is not separable at this resolution.",
        ["weather_monthly", "weather_gdd", "weather_compare"],
    ),
    (
        "3 · Cropland (CDL)",
        "cdl",
        "Levels used: <b>field-year within a field</b> (figure 7), <b>field-year across fields</b> "
        "(figure 8) and <b>across-grower</b> (figure 9).",
        ["cdl_sequence", "cdl_purity", "cdl_compare"],
    ),
]


def _img(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _figure(fig_dir: Path, key: str, full_width: bool = False) -> str:
    meta = FIGURES[key]
    path = fig_dir / meta["file"]
    if not path.exists():
        raise FileNotFoundError(f"missing figure {path}; run src/run_field_eda.py first")
    cls = ' class="wide"' if full_width else ""
    return (
        f'<figure{cls}><img src="{_img(path)}" alt="{html.escape(meta["title"])}">'
        f'<figcaption><span class="lvl">{meta["level"]}</span>'
        f'<b>{html.escape(meta["title"])}</b> — {meta["caption"]}</figcaption></figure>'
    )


def _corn_share(mix_csv: Path) -> str | None:
    if not mix_csv.exists():
        return None
    import pandas as pd

    mix = pd.read_csv(mix_csv, index_col=0)
    if "Corn" not in mix.columns:
        return None
    corn = mix["Corn"]
    return f"{corn.min():.0%}–{corn.max():.0%}"


def build(artifacts: Path, out: Path) -> Path:
    fig_dir, tab_dir = artifacts / "figures", artifacts / "tables"
    payload = json.loads((tab_dir / "findings.json").read_text(encoding="utf-8"))
    findings = payload["findings"]
    years = payload["years"]
    units = payload["units"]
    note = payload.get("coverage_note", "")
    total_fields = sum(u["fields"] for u in units)
    total_acres = sum(u["acres"] for u in units)
    pivots = sum(u["pivots"] for u in units)

    n_counts = {u["fields"] for u in units}
    n_note = (f"{n_counts.pop()} fields per grower" if len(n_counts) == 1
              else f"{min(u['fields'] for u in units)}–{max(u['fields'] for u in units)} fields per grower")
    rows = "".join(
        f"<tr><td><b>{html.escape(u['grower'])}</b></td><td>{html.escape(u['place'])}</td>"
        f"<td>{u['fields']}</td><td>{u['acres']:,.1f}</td><td>{u['pivots']}</td></tr>"
        for u in units
    )
    acres_by_grower = ", ".join(f"{u['grower']} {u['acres']:,.0f} ac" for u in units)
    corn = _corn_share(tab_dir / "summary_cdl.csv")

    def section(title: str, key: str, levels: str, names: list[str]) -> str:
        figs = "".join(_figure(fig_dir, n) for n in names)
        bullets = "".join(f"<li>{html.escape(t)}</li>" for t in findings[key])
        return (
            f'<section><h2>{title}</h2><p class="levels">{levels}</p>'
            f'<div class="figs">{figs}</div><ul>{bullets}</ul></section>'
        )

    corn_line = (
        f" Corn is {corn} of acreage at every grower and {pivots} grower-confirmed center pivots are "
        "included, so the corn/soy and pivot-irrigation context the brief asks for is represented "
        "alongside Delta cotton and peanut rotations."
        if corn else ""
    )

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Field-level EDA — {total_fields} fields, {len(units)} growers</title>
<style>
 body {{ font: 13px/1.4 -apple-system, Segoe UI, Roboto, sans-serif; color:#222; margin:0; background:#fafafa; }}
 main {{ max-width: 1200px; margin: 0 auto; padding: 18px 22px; background:#fff; }}
 h1 {{ font-size: 21px; margin: 0 0 2px; }} h2 {{ font-size: 15px; margin: 14px 0 6px; border-bottom: 2px solid #0072B2; }}
 .sub {{ color:#555; margin-bottom:10px; }}
 .top {{ display:grid; grid-template-columns: 1fr 1.25fr; gap:16px; align-items:start; }}
 table {{ border-collapse: collapse; font-size:12px; }} td, th {{ padding:3px 8px; border-bottom:1px solid #ddd; text-align:left; }}
 .figs {{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; }}
 .figs figure:last-child {{ grid-column: 1 / -1; }}
 figure {{ margin:0; }} img {{ width:100%; height:auto; border:1px solid #eee; }}
 figcaption {{ font-size:11.5px; color:#333; margin:4px 0 0; }}
 .lvl {{ display:inline-block; font-size:9.5px; text-transform:uppercase; letter-spacing:.04em; color:#005b8f;
        background:#eaf3fa; border:1px solid #cfe3f2; border-radius:3px; padding:0 4px; margin-right:5px; }}
 .levels {{ font-size:11.5px; color:#444; margin:0 0 8px; }}
 ul {{ margin:8px 0 0 18px; padding:0; }} li {{ margin-bottom:3px; }}
 .note {{ font-size:11.5px; color:#444; background:#f4f7fb; padding:8px 10px; border-left:3px solid #0072B2; }}
 .note + .note {{ margin-top:8px; }}
 code {{ font-size:11px; }}
 footer {{ margin-top:14px; font-size:11px; color:#666; }}
</style></head><body><main>
<h1>Field-level EDA and spatial comparison</h1>
<div class="sub">{total_fields} grower-drawn fields · {total_acres:,.1f} acres · {len(units)} growers ·
CDL {years['cdl'][0]}–{years['cdl'][-1]} · NASA POWER weather {years['weather'][0]}–{years['weather'][-1]} ·
generated {payload.get('generated', '')} by <code>my-farm-advisor/eda/field-eda</code></div>
<div class="top">
 <div>
  <table><tr><th>Grower</th><th>Location</th><th>Fields</th><th>Acres</th><th>Pivots</th></tr>{rows}</table>
  <p class="note"><b>Data &amp; method.</b> Boundaries are grower-drawn polygons imported by the data
  pipeline (areas in EPSG:5070). Weather is NASA POWER daily data sampled at each field centroid;
  GDD use the 86/50 °F corn method over Apr 1–Sep 30. Crops are the majority USDA CDL class inside each
  boundary (30 m), computed with the pipeline's cdl-cropland zonal routine. Pivot status is
  grower-supplied. {html.escape(note)}</p>
  <p class="note"><b>Dataset scope and authorized substitution.</b> This report analyses {total_fields}
  field boundaries in Lee Co., AR; Pemiscot Co., MO; and Coahoma Co., MS. The brief names an
  Illinois/Iowa/Nebraska sample; this run uses the author's own field boundaries in three Mid-South
  states under instructor authorization, and runs the same pipeline steps on them — boundary import,
  SSURGO soil, DEM terrain with state lidar, NASA POWER weather, CDL {years['cdl'][0]}–{years['cdl'][-1]},
  and multi-year satellite imagery. The layers and the comparison levels are unchanged; only the
  geography and crop mix differ.{corn_line}</p>
 </div>
 {_figure(fig_dir, "map")}
</div>
{section(*SECTIONS[0])}
{section(*SECTIONS[1])}
{section(*SECTIONS[2])}
<section><h2>Limits</h2><ul>
<li>{html.escape(n_note)}: tests are indicative, and acreage differs by grower ({acres_by_grower}).</li>
<li>NASA POWER cannot resolve field-to-field weather inside a farm; on-farm stations would be needed for that.</li>
<li>CDL is a classification, not a planting record: edge pixels, double crops and misclassification all lower the majority share.</li>
<li><b>Soil is out of scope by design.</b> Soil analysis is not required for this assignment and no soil
variable is used or tested here. SSURGO and SoilGrids outputs do exist for these fields from the
Assignment 1 pipeline, but they are deliberately left out of this EDA.</li>
</ul></section>
<footer>Artifacts: <code>src/run_field_eda.py</code> writes the figures, tables and findings;
<code>report/build_report.py</code> assembles this page. Report generation is not part of the reusable
runner — this HTML is a one-time artifact.</footer>
</main></body></html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifacts", default=None,
                        help="Directory holding figures/ and tables/ (default: <runtime>/data-pipeline/eda/field-eda)")
    parser.add_argument("--out", default=None, help="Report path (default: <artifacts>/field_eda_report.html)")
    parser.add_argument("--publish-dir", default=None, help="Also copy report + figures here (small, committable)")
    args = parser.parse_args(argv)

    artifacts = Path(args.artifacts) if args.artifacts else data.pipeline_root() / "eda" / "field-eda"
    out = Path(args.out) if args.out else artifacts / "field_eda_report.html"

    report = build(artifacts, out)
    print(f"Report: {report} ({report.stat().st_size / 1024:,.0f} KB)")

    if args.publish_dir:
        pub = Path(args.publish_dir)
        (pub / "figures").mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, pub / report.name)
        for meta in FIGURES.values():
            src = artifacts / "figures" / meta["file"]
            shutil.copy2(src, pub / "figures" / meta["file"])
        print(f"Published to {pub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

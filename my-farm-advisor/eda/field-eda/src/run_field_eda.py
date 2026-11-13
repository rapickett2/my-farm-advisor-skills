#!/usr/bin/env python3
"""Field-level EDA across grower units: boundaries, weather, CDL, and a map.

Reads data-pipeline runtime outputs only, writes static PNG figures, CSV
tables, and one self-contained single-page HTML report under
``${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/field-eda/`` (or ``--out-dir``).
``--publish-dir`` additionally copies the report and figures somewhere small
enough to commit (see README).

Example:
    python run_field_eda.py \\
        --unit wrightland-brothers:wrightland --unit steele-cooter-farms:holland \\
        --unit coahoma:coahoma --cdl-years 2019-2025 --weather-years 2021-2025
"""

from __future__ import annotations

import argparse
import base64
import html
import shutil
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import field_eda_data as data  # noqa: E402
import field_eda_plots as plots  # noqa: E402


def _years(text: str) -> list[int]:
    start, _, end = text.partition("-")
    return list(range(int(start), int(end or start) + 1))


def _states(base: Path, fips: set[str]) -> gpd.GeoDataFrame | None:
    path = base / "shared" / "geoadmin" / "l2_counties" / "counties_usa.geojson"
    if not path.exists():
        return None
    counties = gpd.read_file(path)
    counties["state_fips"] = counties["state_fips"].astype(str).str.zfill(2)
    # Draw the target states plus their neighbours inside the locator window.
    states = counties.dissolve(by="state_fips", as_index=False)[["state_fips", "geometry"]]
    abbr = {"01": "AL", "05": "AR", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
            "22": "LA", "28": "MS", "29": "MO", "40": "OK", "47": "TN", "48": "TX"}
    states["state_abbr"] = states["state_fips"].map(abbr).fillna("")
    return states[states["state_abbr"] != ""]


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _img(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_findings(fields, bsum, wsum, spread, mix, dom) -> dict[str, list[str]]:
    """Plain-language findings computed from the summary tables."""
    out: dict[str, list[str]] = {"boundaries": [], "weather": [], "cdl": []}
    med = bsum["median_acres"].sort_values()
    p = bsum.attrs.get("kruskal_p", {})
    per = bsum["fields"]
    n_text = f"{per.iloc[0]} fields per grower" if per.nunique() == 1 else f"{per.min()}–{per.max()} fields per grower"
    out["boundaries"].append(
        f"Median field size runs from {med.iloc[0]:.0f} ac ({med.index[0]}) to {med.iloc[-1]:.0f} ac "
        f"({med.index[-1]}); across all {len(fields)} fields it spans "
        f"{fields['acres'].min():.1f}–{fields['acres'].max():.1f} ac. Kruskal–Wallis p = "
        f"{p.get('acres', np.nan):.3f}, so with {n_text} the size difference "
        f"{'is' if p.get('acres', 1) < 0.05 else 'is not'} statistically clear."
    )
    rect = bsum["median_rectangularity"].sort_values()
    out["boundaries"].append(
        f"{rect.index[-1]} fields are the most regular (median rectangularity {rect.iloc[-1]:.2f}) and "
        f"{rect.index[0]} the least ({rect.iloc[0]:.2f}; p = {p.get('rectangularity', np.nan):.3f}). "
        f"Small fields carry the most boundary per acre, i.e. the most headland and turn-row."
    )
    piv = ", ".join(f"{g} {r.pivots:.0f} ({_pct(r.pivot_share)} of acres)" for g, r in bsum.iterrows())
    out["boundaries"].append(f"Center pivots (grower-confirmed): {piv}.")

    gdd = wsum[("gdd_f", "mean")].sort_values()
    rain = wsum[("precip_in", "mean")].sort_values()
    hot = wsum[("hot_days", "mean")].sort_values()
    centers = fields.groupby("grower")[["lat", "lon"]].mean()
    lo, hi = centers.loc[gdd.index[0]], centers.loc[gdd.index[-1]]
    km = 6371 * np.arccos(
        np.clip(
            np.sin(np.radians(lo.lat)) * np.sin(np.radians(hi.lat))
            + np.cos(np.radians(lo.lat)) * np.cos(np.radians(hi.lat)) * np.cos(np.radians(hi.lon - lo.lon)),
            -1, 1,
        )
    )
    out["weather"].append(
        f"Apr–Sep GDD averages {gdd.iloc[0]:,.0f} °F ({gdd.index[0]}, {lo.lat:.2f}°N) to "
        f"{gdd.iloc[-1]:,.0f} °F ({gdd.index[-1]}, {hi.lat:.2f}°N): a {gdd.iloc[-1] - gdd.iloc[0]:,.0f} "
        f"°F-day gap between farms {km:.0f} km apart."
    )
    year_swing = (wsum[("precip_in", "max")] - wsum[("precip_in", "min")]).median()
    between = rain.iloc[-1] - rain.iloc[0]
    out["weather"].append(
        f"Season rainfall averages {rain.iloc[0]:.1f} in ({rain.index[0]}) to {rain.iloc[-1]:.1f} in "
        f"({rain.index[-1]}); days ≥ 95 °F average {hot.iloc[0]:.0f} ({hot.index[0]}) to "
        f"{hot.iloc[-1]:.0f} ({hot.index[-1]}). Rainfall swings {year_swing:.1f} in between a grower's "
        f"wettest and driest season, "
        + ("more than" if year_swing > between else "less than")
        + f" the {between:.1f} in gap between growers."
    )
    max_spread = spread["gdd_f_field_range"].max()
    if max_spread < 25:
        out["weather"].append(
            f"Within each farm, fields differ by at most {max_spread:.0f} GDD in any season: NASA POWER "
            f"meteorology is a 0.5° × 0.625° grid, so a farm's fields share one or two cells. Weather "
            f"here compares growers, not fields within a farm."
        )
    else:
        out["weather"].append(
            f"Within-farm field spread reaches {max_spread:.0f} GDD in a season, so some farms straddle "
            f"NASA POWER grid cells; compare that with the gap between growers before reading it as "
            f"real field-to-field climate."
        )

    dominant = mix.drop(columns="rotation_rate").idxmax(axis=1)
    share = mix.drop(columns="rotation_rate").max(axis=1)
    out["cdl"].append(
        "Acre-weighted crop mix, all years: "
        + "; ".join(f"{g} mostly {dominant[g]} ({_pct(share[g])})" for g in mix.index)
        + "."
    )
    rot = mix["rotation_rate"].sort_values()
    out["cdl"].append(
        f"The crop changes from the previous year on {_pct(rot.iloc[0])} of acres per year at "
        f"{rot.index[0]} versus {_pct(rot.iloc[-1])} at {rot.index[-1]}."
    )
    low = dom.groupby("field_name")["crop_pct"].median().sort_values()
    out["cdl"].append(
        f"The majority crop covers a median {dom['crop_pct'].median():.0f}% of a field-year. Lowest: "
        + ", ".join(f"{n} ({v:.0f}%)" for n, v in low.head(3).items())
        + " — mixed labels come from 30 m edge pixels on small fields and from split or double-cropped fields."
    )
    return out


def write_report(path: Path, figs: dict[str, Path], fields, findings, years, coverage_note) -> Path:
    counts = fields.groupby("grower")["field_id"].size()
    n_note = (f"{counts.iloc[0]} fields per grower" if counts.nunique() == 1
              else f"{counts.min()}–{counts.max()} fields per grower")
    units = (
        fields.groupby("grower")
        .agg(place=("place", "first"), n=("field_id", "size"), ac=("acres", "sum"), piv=("pivot", "sum"))
        .reset_index()
    )
    rows = "".join(
        f"<tr><td><b>{html.escape(r.grower)}</b></td><td>{html.escape(r.place)}</td><td>{r.n}</td>"
        f"<td>{r.ac:,.1f}</td><td>{r.piv}</td></tr>"
        for r in units.itertuples()
    )

    def section(title: str, key: str, names: list[str]) -> str:
        imgs = "".join(
            f'<figure><img src="{_img(figs[n])}" alt="{html.escape(n)}"></figure>' for n in names
        )
        bullets = "".join(f"<li>{html.escape(t)}</li>" for t in findings[key])
        return f'<section><h2>{title}</h2><div class="figs">{imgs}</div><ul>{bullets}</ul></section>'

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Field-level EDA — {len(fields)} fields, {fields['grower'].nunique()} growers</title>
<style>
 body {{ font: 13px/1.4 -apple-system, Segoe UI, Roboto, sans-serif; color:#222; margin:0; background:#fafafa; }}
 main {{ max-width: 1200px; margin: 0 auto; padding: 18px 22px; background:#fff; }}
 h1 {{ font-size: 21px; margin: 0 0 2px; }} h2 {{ font-size: 15px; margin: 14px 0 6px; border-bottom: 2px solid #0072B2; }}
 .sub {{ color:#555; margin-bottom:10px; }}
 .top {{ display:grid; grid-template-columns: 1fr 1.25fr; gap:16px; align-items:start; }}
 table {{ border-collapse: collapse; font-size:12px; }} td, th {{ padding:3px 8px; border-bottom:1px solid #ddd; text-align:left; }}
 .figs {{ display:grid; grid-template-columns: 1fr 1fr; gap:8px; }}
 .figs figure:last-child {{ grid-column: 1 / -1; }}
 figure {{ margin:0; }} img {{ width:100%; height:auto; border:1px solid #eee; }}
 ul {{ margin:6px 0 0 18px; padding:0; }} li {{ margin-bottom:3px; }}
 .note {{ font-size:11.5px; color:#444; background:#f4f7fb; padding:8px 10px; border-left:3px solid #0072B2; }}
</style></head><body><main>
<h1>Field-level EDA and spatial comparison</h1>
<div class="sub">{len(fields)} grower-drawn fields · {fields['acres'].sum():,.1f} acres ·
CDL {years['cdl'][0]}–{years['cdl'][-1]} · NASA POWER weather {years['weather'][0]}–{years['weather'][-1]} ·
generated {date.today().isoformat()} by <code>my-farm-advisor/eda/field-eda</code></div>
<div class="top">
 <div>
  <table><tr><th>Grower</th><th>Location</th><th>Fields</th><th>Acres</th><th>Pivots</th></tr>{rows}</table>
  <p class="note"><b>Data &amp; method.</b> Boundaries are grower-drawn polygons imported by the data
  pipeline (areas in EPSG:5070). Weather is NASA POWER daily data sampled at each field centroid;
  GDD use the 86/50 °F corn method over Apr 1–Sep 30. Crops are the majority USDA CDL class inside each
  boundary (30 m), computed with the pipeline's cdl-cropland zonal routine. Pivot status is
  grower-supplied. Soil is out of scope for this report by design. {html.escape(coverage_note)}</p>
 </div>
 <figure><img src="{_img(figs['map'])}" alt="field map"></figure>
</div>
{section("1 · Field boundaries", "boundaries", ["boundaries_size", "boundaries_shape", "boundaries_compare"])}
{section("2 · Weather", "weather", ["weather_monthly", "weather_gdd", "weather_compare"])}
{section("3 · Cropland (CDL)", "cdl", ["cdl_sequence", "cdl_purity", "cdl_compare"])}
<section><h2>Limits</h2><ul>
<li>{html.escape(n_note)}: tests are indicative, and acreage differs by grower ({', '.join(f"{g} {a:,.0f} ac" for g, a in fields.groupby('grower')['acres'].sum().items())}).</li>
<li>NASA POWER cannot resolve field-to-field weather inside a farm; on-farm stations would be needed for that.</li>
<li>CDL is a classification, not a planting record: edge pixels, double crops and misclassification all lower the majority share.</li>
</ul></section>
</main></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--unit", action="append", required=True, help="<grower-slug>:<farm-slug> (repeatable)")
    parser.add_argument("--cdl-years", default="2019-2025")
    parser.add_argument("--weather-years", default="2021-2025")
    parser.add_argument("--out-dir", default=None, help="Default: <runtime>/data-pipeline/eda/field-eda")
    parser.add_argument("--publish-dir", default=None, help="Also copy report + figures here (small, committable)")
    parser.add_argument("--no-basemap", action="store_true", help="Skip online imagery tiles on the map")
    args = parser.parse_args(argv)

    base = data.pipeline_root()
    out = Path(args.out_dir) if args.out_dir else base / "eda" / "field-eda"
    fig_dir, tab_dir = out / "figures", out / "tables"
    units = [data.FarmUnit.parse(u) for u in args.unit]
    cdl_years, weather_years = _years(args.cdl_years), _years(args.weather_years)

    print("Loading fields …")
    fields = data.load_fields(base, units)
    print(f"  {len(fields)} fields, {fields['acres'].sum():,.1f} ac, growers: {sorted(fields['grower'].unique())}")
    print("Loading weather …")
    weather = data.load_weather(base, fields, weather_years[0], weather_years[-1])
    print(f"  {len(weather):,} daily rows")
    print("Computing CDL composition …")
    cdl = data.load_cdl(base, fields, cdl_years)
    dom = data.dominant_crops(cdl)
    print(f"  {len(dom)} field-years")
    if weather.empty or dom.empty:
        print("ERROR: weather or CDL inputs missing; run the data pipeline first")
        return 1
    season = data.season_weather(weather)
    spread = data.within_unit_spread(season, "gdd_f")

    figs = {
        "boundaries_size": plots.boundaries_size(fields, fig_dir / "01_boundaries_size.png"),
        "boundaries_shape": plots.boundaries_shape(fields, fig_dir / "02_boundaries_shape.png"),
        "weather_monthly": plots.weather_monthly(weather, fig_dir / "04_weather_monthly.png"),
        "weather_gdd": plots.weather_gdd(weather, fig_dir / "05_weather_gdd.png"),
        "cdl_sequence": plots.cdl_sequence(dom, fields, fig_dir / "07_cdl_sequence.png"),
        "cdl_purity": plots.cdl_purity(dom, fig_dir / "08_cdl_purity.png"),
    }
    figs["boundaries_compare"], bsum = plots.boundaries_compare(fields, fig_dir / "03_boundaries_compare.png")
    figs["weather_compare"], wsum = plots.weather_compare(season, fig_dir / "06_weather_compare.png")
    figs["cdl_compare"], mix = plots.cdl_compare(dom, fig_dir / "09_cdl_compare.png")
    states = _states(base, set(fields["state_fips"].astype(str)))
    figs["map"] = plots.field_map(fields, dom, states, fig_dir / "10_field_map.png", basemap=not args.no_basemap)

    tab_dir.mkdir(parents=True, exist_ok=True)
    fields.drop(columns="geometry").to_csv(tab_dir / "fields.csv", index=False)
    season.to_csv(tab_dir / "season_weather.csv", index=False)
    dom.to_csv(tab_dir / "cdl_majority_crop.csv", index=False)
    cdl.to_csv(tab_dir / "cdl_composition.csv", index=False)
    bsum.to_csv(tab_dir / "summary_boundaries.csv")
    wsum.to_csv(tab_dir / "summary_weather.csv")
    mix.to_csv(tab_dir / "summary_cdl.csv")
    spread.to_csv(tab_dir / "weather_within_farm_spread.csv", index=False)

    missing = sorted(set(cdl_years) - set(dom["year"]))
    note = f"CDL years without rasters: {missing}." if missing else ""
    findings = build_findings(fields, bsum, wsum, spread, mix, dom)
    report = write_report(out / "field_eda_report.html", figs, fields, findings,
                          {"cdl": cdl_years, "weather": weather_years}, note)
    print(f"\nReport: {report} ({report.stat().st_size / 1024:,.0f} KB)")
    for key, lines in findings.items():
        print(f"\n[{key}]")
        for line in lines:
            print(f"  - {line}")

    if args.publish_dir:
        pub = Path(args.publish_dir)
        (pub / "figures").mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, pub / report.name)
        for p in figs.values():
            shutil.copy2(p, pub / "figures" / p.name)
        print(f"\nPublished to {pub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

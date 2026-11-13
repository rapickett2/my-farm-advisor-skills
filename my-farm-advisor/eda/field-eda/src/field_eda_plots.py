"""Static matplotlib figures for the field-level EDA (no interactive output)."""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from scipy import stats  # noqa: E402

# Okabe-Ito, colour-blind safe, one per grower unit (assigned in sorted order).
GROWER_COLORS = ["#E69F00", "#0072B2", "#009E73", "#CC79A7", "#56B4E9", "#D55E00"]
# USDA CDL legend colours so crops read the same as CropScape.
CROP_COLORS = {
    "Corn": "#FFD300",
    "Cotton": "#FF2626",
    "Rice": "#00A8E2",
    "Sorghum": "#FF9E0A",
    "Soybeans": "#267000",
    "Peanuts": "#70A500",
    "Winter Wheat": "#A57000",
    "Dbl Crop WinWht/Soybeans": "#707000",
    "Fallow/Idle Cropland": "#BFBF77",
}
OTHER_COLOR = "#9E9E9E"
DPI = 110
MONTHS = "J F M A M J J A S O N D".split()

plt.rcParams.update(
    {
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": DPI,
        "savefig.dpi": DPI,
        "savefig.bbox": "tight",
    }
)


def grower_palette(growers) -> dict[str, str]:
    return {g: GROWER_COLORS[i % len(GROWER_COLORS)] for i, g in enumerate(sorted(growers))}


def crop_color(crop: str) -> str:
    return CROP_COLORS.get(crop, OTHER_COLOR)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path


def _plain_log_x(ax) -> None:
    """Log x axis labelled in plain acres (10, 20, 50, 100, 200) instead of 3×10¹."""
    ax.set_xscale("log")
    ax.xaxis.set_major_locator(matplotlib.ticker.FixedLocator([5, 10, 20, 30, 50, 100, 200, 300, 500]))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def _unit_titles(fields: pd.DataFrame) -> dict[str, str]:
    return (
        fields.groupby("grower")
        .agg(place=("place", "first"), n=("field_id", "size"), ac=("acres", "sum"))
        .apply(lambda r: f"{r.place} · {r.n} fields · {r.ac:,.0f} ac", axis=1)
        .to_dict()
    )


# ----------------------------------------------------------------- boundaries
def boundaries_size(fields: pd.DataFrame, path: Path) -> Path:
    """V1: every field's acreage, grouped by grower, pivots marked."""
    pal = grower_palette(fields["grower"].unique())
    df = fields.sort_values(["grower", "acres"], ascending=[True, True]).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(6.4, 6.2))
    y = np.arange(len(df))
    ax.barh(
        y, df["acres"], color=[pal[g] for g in df["grower"]],
        hatch=["///" if p else "" for p in df["pivot"]], edgecolor="#222", linewidth=0.4,
    )
    ax.set_yticks(y, df["field_name"], fontsize=7)
    for yi, (acres, pivot) in enumerate(zip(df["acres"], df["pivot"])):
        ax.text(acres + 2, yi, f"{acres:.0f}" + (" · pivot" if pivot else ""), va="center", fontsize=6.5)
    ax.set_xlabel("Field area (acres)")
    ax.set_title("Field size, all fields")
    handles = [Patch(color=pal[g], label=g) for g in sorted(pal)]
    handles.append(Patch(facecolor="white", edgecolor="#222", hatch="///", label="Center pivot"))
    ax.legend(handles=handles, loc="lower right", frameon=False)
    ax.set_xlim(0, df["acres"].max() * 1.22)
    return _save(fig, path)


def boundaries_shape(fields: pd.DataFrame, path: Path) -> Path:
    """V2: boundary length per acre vs size, against the square-field minimum."""
    pal = grower_palette(fields["grower"].unique())
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    acres = np.linspace(fields["acres"].min() * 0.8, fields["acres"].max() * 1.1, 200)
    square = 4 * np.sqrt(acres / (1 / 4046.8564224)) / acres  # perimeter of a square / acres
    ax.plot(acres, square, color="#555", lw=1, ls="--", label="Square field (minimum edge)")
    for grower, group in fields.groupby("grower"):
        ax.scatter(
            group["acres"], group["edge_m_per_acre"], s=40, color=pal[grower], edgecolor="#222",
            linewidth=0.5, marker="o", label=grower, zorder=3,
        )
        piv = group[group["pivot"]]
        ax.scatter(piv["acres"], piv["edge_m_per_acre"], s=130, facecolor="none",
                   edgecolor="#222", linewidth=1.2, zorder=4)
    for row in fields.itertuples():
        ratio = row.edge_m_per_acre / (4 * np.sqrt(row.acres * 4046.8564224) / row.acres)
        if ratio > 1.45 or row.acres < 15:
            ax.annotate(row.field_name, (row.acres, row.edge_m_per_acre), fontsize=6.5,
                        xytext=(4, 3), textcoords="offset points")
    _plain_log_x(ax)
    ax.set_xlabel("Field area (acres, log scale)")
    ax.set_ylabel("Boundary length per acre (m/ac)")
    ax.set_title("Edge per acre: small and irregular fields carry more headland")
    handles, labels = ax.get_legend_handles_labels()
    handles.append(Line2D([], [], marker="o", ls="", markerfacecolor="none", markeredgecolor="#222",
                          markersize=10, label="Center pivot"))
    ax.legend(handles=handles, frameon=False, fontsize=7.5)
    return _save(fig, path)


def boundaries_compare(fields: pd.DataFrame, path: Path) -> tuple[Path, pd.DataFrame]:
    """C1: grower distributions of size and rectangularity + Kruskal-Wallis."""
    pal = grower_palette(fields["grower"].unique())
    growers = sorted(fields["grower"].unique())
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.4))
    metrics = [("acres", "Field area (acres)"), ("rectangularity", "Rectangularity (1 = rectangle)"),
               ("edge_m_per_acre", "Edge per acre (m/ac)")]
    tests = {}
    rng = np.random.default_rng(0)
    for ax, (col, label) in zip(axes, metrics):
        data = [fields.loc[fields["grower"] == g, col].to_numpy() for g in growers]
        box = ax.boxplot(data, tick_labels=growers, widths=0.5, patch_artist=True, showfliers=False)
        for patch, g in zip(box["boxes"], growers):
            patch.set(facecolor=pal[g], alpha=0.35, edgecolor="#222")
        for i, (g, vals) in enumerate(zip(growers, data), 1):
            ax.scatter(i + rng.uniform(-0.12, 0.12, len(vals)), vals, s=14, color=pal[g],
                       edgecolor="#222", linewidth=0.4, zorder=3)
        ok = len(data) >= 2 and all(len(d) > 1 for d in data)
        h, p = stats.kruskal(*data) if ok else (np.nan, np.nan)
        tests[col] = p
        ax.set_title(f"{label}\n" + (f"Kruskal–Wallis p = {p:.3f}" if ok else "one group: no test"), fontsize=9)
    fig.suptitle("Grower comparison — field boundaries", fontweight="bold", y=1.04)
    summary = fields.groupby("grower").agg(
        fields=("field_id", "size"),
        total_acres=("acres", "sum"),
        median_acres=("acres", "median"),
        cv_acres=("acres", lambda s: s.std() / s.mean()),
        median_rectangularity=("rectangularity", "median"),
        median_edge_m_per_acre=("edge_m_per_acre", "median"),
        pivots=("pivot", "sum"),
        pivot_acres=("acres", lambda s: s[fields.loc[s.index, "pivot"]].sum()),
    )
    summary["pivot_share"] = summary["pivot_acres"] / summary["total_acres"]
    summary.attrs["kruskal_p"] = tests
    return _save(fig, path), summary


# -------------------------------------------------------------------- weather
def weather_monthly(weather: pd.DataFrame, path: Path) -> Path:
    """V1: mean monthly precipitation and Tmax, one series per grower."""
    pal = grower_palette(weather["grower"].unique())
    per_field = (
        weather.groupby(["grower", "field_id", "year", "month"])
        .agg(precip_in=("precip_in", "sum"), tmax_f=("tmax_f", "mean"))
        .reset_index()
    )
    per_unit = per_field.groupby(["grower", "month"])[["precip_in", "tmax_f"]].mean().reset_index()
    growers = sorted(per_unit["grower"].unique())
    width = 0.8 / len(growers)
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    ax2 = ax.twinx()
    for i, g in enumerate(growers):
        d = per_unit[per_unit["grower"] == g]
        ax.bar(d["month"] + (i - (len(growers) - 1) / 2) * width, d["precip_in"], width=width,
               color=pal[g], alpha=0.85, label=f"{g} precip")
        ax2.plot(d["month"], d["tmax_f"], color=pal[g], lw=1.6, marker="o", ms=3, label=f"{g} Tmax")
    ax.set_xticks(range(1, 13), MONTHS)
    ax.set_ylabel("Mean monthly precipitation (in)")
    ax2.set_ylabel("Mean daily Tmax (°F)")
    ax2.spines["right"].set_visible(True)
    years = f"{weather['year'].min()}–{weather['year'].max()}"
    ax.set_title(f"Monthly climate, {years} mean (NASA POWER)")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, ncol=2, fontsize=7, frameon=False, loc="upper left")
    ax.set_ylim(0, per_unit["precip_in"].max() * 1.6)
    return _save(fig, path)


def weather_gdd(weather: pd.DataFrame, path: Path, start_month: int = 4, end_month: int = 9) -> Path:
    """V2: cumulative growing-season GDD per year, one panel per grower."""
    growers = sorted(weather["grower"].unique())
    season = weather[weather["month"].between(start_month, end_month)]
    unit_daily = season.groupby(["grower", "year", "date"])["gdd_f"].mean().reset_index()
    unit_daily["doy"] = unit_daily["date"].dt.dayofyear
    unit_daily["cum"] = unit_daily.groupby(["grower", "year"])["gdd_f"].cumsum()
    years = sorted(unit_daily["year"].unique())
    cmap = plt.get_cmap("viridis", len(years))
    fig, axes = plt.subplots(1, len(growers), figsize=(10, 3.2), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, g in zip(axes, growers):
        for j, yr in enumerate(years):
            d = unit_daily[(unit_daily["grower"] == g) & (unit_daily["year"] == yr)]
            ax.plot(d["doy"], d["cum"], color=cmap(j), lw=1.4, label=str(yr))
        ax.set_title(g)
        ax.set_xlabel("Day of year")
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("Cumulative GDD (°F, 86/50)")
    axes[-1].legend(frameon=False, fontsize=7, loc="upper left")
    fig.suptitle(f"Growing-degree-day accumulation, {MONTHS[start_month-1]}–{MONTHS[end_month-1]} "
                 "(Apr 1 – Sep 30)", fontweight="bold", y=1.03)
    return _save(fig, path)


def weather_compare(season: pd.DataFrame, path: Path) -> tuple[Path, pd.DataFrame]:
    """C1: season GDD, rainfall and ≥95 °F days by year, growers side by side."""
    pal = grower_palette(season["grower"].unique())
    growers = sorted(season["grower"].unique())
    unit = season.groupby(["grower", "year"])[["gdd_f", "precip_in", "hot_days"]].mean().reset_index()
    years = sorted(unit["year"].unique())
    width = 0.8 / len(growers)
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.2))
    for ax, (col, label) in zip(axes, [("gdd_f", "Season GDD (°F)"),
                                        ("precip_in", "Season rainfall (in)"),
                                        ("hot_days", "Days with Tmax ≥ 95 °F")]):
        for i, g in enumerate(growers):
            d = unit[unit["grower"] == g].set_index("year").reindex(years)
            ax.bar(np.arange(len(years)) + (i - (len(growers) - 1) / 2) * width, d[col], width=width,
                   color=pal[g], label=g, edgecolor="#222", linewidth=0.3)
        ax.set_xticks(range(len(years)), years)
        ax.set_title(label, fontsize=9)
    axes[0].legend(frameon=False, fontsize=7)
    fig.suptitle("Grower comparison — growing season (Apr–Sep) weather by year",
                 fontweight="bold", y=1.04)
    summary = unit.groupby("grower")[["gdd_f", "precip_in", "hot_days"]].agg(["mean", "min", "max"])
    return _save(fig, path), summary


# ------------------------------------------------------------------------ CDL
def cdl_sequence(dom: pd.DataFrame, fields: pd.DataFrame, path: Path) -> Path:
    """V1: crop-by-year grid for every field (rotation chart)."""
    years = sorted(dom["year"].unique())
    order = fields.sort_values(["grower", "field_name"])[["field_id", "field_name", "grower", "pivot"]]
    grid = dom.pivot_table(index="field_id", columns="year", values="crop", aggfunc="first")
    grid = grid.reindex(order["field_id"])
    present = [c for c in pd.unique(grid.to_numpy().ravel()) if isinstance(c, str)]
    crops = sorted(present, key=lambda c: (c not in CROP_COLORS, c))
    codes = {c: i for i, c in enumerate(crops)}
    mat = grid.apply(lambda col: col.map(codes)).to_numpy(dtype=float)
    cmap = matplotlib.colors.ListedColormap([crop_color(c) for c in crops])
    fig, ax = plt.subplots(figsize=(6.4, 7.0))
    ax.imshow(np.ma.masked_invalid(mat), cmap=cmap, vmin=-0.5, vmax=len(crops) - 0.5, aspect="auto")
    labels = [f"{n}{'  ◉' if p else ''}" for n, p in zip(order["field_name"], order["pivot"])]
    ax.set_yticks(range(len(order)), labels, fontsize=7)
    ax.set_xticks(range(len(years)), years)
    ax.set_xticks(np.arange(-0.5, len(years)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(order)), minor=True)
    ax.grid(which="minor", color="white", lw=0.8)
    ax.tick_params(which="minor", length=0)
    for b in np.flatnonzero(order["grower"].to_numpy()[1:] != order["grower"].to_numpy()[:-1]):
        ax.axhline(b + 0.5, color="#222", lw=2)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.legend(handles=[Patch(color=crop_color(c), label=c) for c in crops], ncol=2, fontsize=7,
              frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.04))
    ax.set_title(f"Majority CDL crop per field, {years[0]}–{years[-1]}  (◉ = center pivot)")
    return _save(fig, path)


def cdl_purity(dom: pd.DataFrame, path: Path) -> Path:
    """V2: how much of each field the majority crop covers, against field size."""
    pal = grower_palette(dom["grower"].unique())
    per_field = dom.groupby(["grower", "field_id", "field_name", "acres"])["crop_pct"].median().reset_index()
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    for g, d in dom.groupby("grower"):
        ax.scatter(d["acres"], d["crop_pct"], s=10, color=pal[g], alpha=0.35, linewidth=0)
    for g, d in per_field.groupby("grower"):
        ax.scatter(d["acres"], d["crop_pct"], s=42, color=pal[g], edgecolor="#222", linewidth=0.6,
                   label=f"{g} (field median)", zorder=3)
    for row in per_field[per_field["crop_pct"] < 80].itertuples():
        ax.annotate(row.field_name, (row.acres, row.crop_pct), fontsize=6.5, xytext=(4, -3),
                    textcoords="offset points")
    rho, p = stats.spearmanr(per_field["acres"], per_field["crop_pct"])
    _plain_log_x(ax)
    ax.set_ylim(min(40, dom["crop_pct"].min() - 5), 101)
    ax.set_xlabel("Field area (acres, log scale)")
    ax.set_ylabel("Majority-crop share of field (%)")
    ax.set_title(f"CDL label purity vs field size (Spearman ρ = {rho:.2f}, p = {p:.3f})")
    ax.legend(frameon=False, fontsize=7, loc="lower right")
    ax.text(0.01, 0.02, "faint dots: individual field-years", transform=ax.transAxes, fontsize=6.5,
            color="#555")
    return _save(fig, path)


def cdl_compare(dom: pd.DataFrame, path: Path) -> tuple[Path, pd.DataFrame]:
    """C1: crop acreage mix by year for each grower, plus rotation rate."""
    growers = sorted(dom["grower"].unique())
    years = sorted(dom["year"].unique())
    crops = sorted(dom["crop"].unique(), key=lambda c: (c not in CROP_COLORS, c))
    fig, axes = plt.subplots(1, len(growers), figsize=(10, 3.4), sharey=True)
    axes = np.atleast_1d(axes)
    seq = dom.sort_values(["field_id", "year"]).copy()
    seq["changed"] = seq.groupby("field_id")["crop"].shift().ne(seq["crop"]) & seq.groupby("field_id")["crop"].shift().notna()
    rotation = (
        seq[seq.groupby("field_id").cumcount() > 0]
        .groupby("grower")
        .apply(lambda d: np.average(d["changed"], weights=d["acres"]), include_groups=False)
    )
    for ax, g in zip(axes, growers):
        d = dom[dom["grower"] == g]
        share = d.pivot_table(index="year", columns="crop", values="acres", aggfunc="sum").reindex(years).fillna(0)
        share = share.div(share.sum(axis=1), axis=0) * 100
        bottom = np.zeros(len(years))
        for c in crops:
            if c in share:
                ax.bar(range(len(years)), share[c], bottom=bottom, color=crop_color(c), edgecolor="white",
                       linewidth=0.4, label=c)
                bottom += share[c].to_numpy()
        ax.set_xticks(range(len(years)), [str(y)[2:] for y in years])
        ax.set_title(f"{g} — crop changes on {rotation.get(g, np.nan):.0%} of acres/yr", fontsize=9)
    axes[0].set_ylabel("Share of grower acres (%)")
    handles = [Patch(color=crop_color(c), label=c) for c in crops]
    fig.legend(handles=handles, ncol=min(6, len(crops)), frameon=False, fontsize=7,
               loc="lower center", bbox_to_anchor=(0.5, -0.1))
    fig.suptitle("Grower comparison — crop mix by year (majority CDL crop, acre-weighted)",
                 fontweight="bold", y=1.04)
    mix = dom.groupby(["grower", "crop"])["acres"].sum().unstack(fill_value=0)
    mix = mix.div(mix.sum(axis=1), axis=0)
    mix["rotation_rate"] = rotation
    return _save(fig, path), mix


# ------------------------------------------------------------------------ map
def field_map(fields: gpd.GeoDataFrame, dom: pd.DataFrame, states: gpd.GeoDataFrame | None, path: Path,
              basemap: bool = True) -> Path:
    """Geospatial map: each grower's fields by latest crop on imagery, plus a locator."""
    growers = sorted(fields["grower"].unique())
    latest = dom["year"].max() if not dom.empty else None
    crop_by_field = (
        dom[dom["year"] == latest].set_index("field_id")["crop"] if latest is not None else pd.Series(dtype=str)
    )
    web = fields.to_crs("EPSG:3857").copy()
    web["crop"] = web["field_id"].map(crop_by_field).fillna("No data")
    pal = grower_palette(growers)
    fig = plt.figure(figsize=(10, 7.4))
    gs = fig.add_gridspec(2, 2, wspace=0.06, hspace=0.14)
    slots = [gs[0, 0], gs[0, 1], gs[1, 0]]
    titles = _unit_titles(fields)
    for slot, g in zip(slots, growers):
        ax = fig.add_subplot(slot)
        d = web[web["grower"] == g]
        d.plot(ax=ax, facecolor=[crop_color(c) for c in d["crop"]], alpha=0.55, edgecolor="none")
        d.boundary.plot(ax=ax, color="white", linewidth=1.6)
        d[d["pivot"]].boundary.plot(ax=ax, color="#111", linewidth=1.3, linestyle=(0, (3, 2)))
        for row in d.itertuples():
            pt = row.geometry.representative_point()
            ax.annotate(row.field_name.split()[-1], (pt.x, pt.y), ha="center", va="center", fontsize=7,
                        fontweight="bold", color="white",
                        bbox=dict(boxstyle="round,pad=0.15", fc="#000", alpha=0.55, lw=0))
        minx, miny, maxx, maxy = d.total_bounds
        pad = max(maxx - minx, maxy - miny) * 0.08
        ax.set_xlim(minx - pad, maxx + pad)
        ax.set_ylim(miny - pad, maxy + pad)
        if basemap:
            try:
                import contextily as cx

                cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, attribution_size=5)
            except Exception as exc:  # network optional: the map still renders without tiles
                warnings.warn(f"basemap unavailable: {exc}")
        ax.set_axis_off()
        ax.set_title(f"{g} — {titles[g]}", fontsize=9, color=pal[g])
        # 500 m scale bar, bottom-right (attribution sits bottom-left).
        # Web Mercator metres are stretched by 1/cos(lat).
        lat = float(fields.loc[fields["grower"] == g, "lat"].mean())
        bar = 500 / np.cos(np.radians(lat))
        x1, y0 = maxx + pad * 0.6, miny - pad * 0.45
        ax.plot([x1 - bar, x1], [y0, y0], color="white", lw=3, solid_capstyle="butt")
        ax.text(x1 - bar / 2, y0 + pad * 0.2, "500 m", color="white", ha="center", fontsize=7,
                fontweight="bold")

    loc = fig.add_subplot(gs[1, 1])
    if states is not None and not states.empty:
        states.to_crs("EPSG:3857").plot(ax=loc, facecolor="#f2f2f2", edgecolor="#777", linewidth=0.6)
        for row in states.to_crs("EPSG:3857").itertuples():
            pt = row.geometry.representative_point()
            loc.text(pt.x, pt.y, row.state_abbr, fontsize=8, color="#888", ha="center", clip_on=True)
    pts = gpd.GeoDataFrame(
        fields.groupby("grower")[["lon", "lat"]].mean().reset_index(),
        geometry=gpd.points_from_xy(*fields.groupby("grower")[["lon", "lat"]].mean().to_numpy().T),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")
    for row in pts.itertuples():
        loc.scatter(row.geometry.x, row.geometry.y, s=90, color=pal[row.grower], edgecolor="#222",
                    zorder=3)
        loc.annotate(row.grower, (row.geometry.x, row.geometry.y), xytext=(7, 4),
                     textcoords="offset points", fontsize=8, fontweight="bold")
    minx, miny, maxx, maxy = pts.total_bounds
    span = max(maxx - minx, maxy - miny)
    loc.set_xlim(minx - span * 0.9, maxx + span * 0.9)
    loc.set_ylim(miny - span * 0.7, maxy + span * 0.7)
    loc.set_xticks([])
    loc.set_yticks([])
    loc.set_xlabel("")
    loc.set_ylabel("")
    loc.set_title("Locator — Mid-South", fontsize=9)
    crops = sorted(set(web["crop"]), key=lambda c: (c not in CROP_COLORS, c))
    handles = [Patch(color=crop_color(c), alpha=0.7, label=c) for c in crops]
    handles.append(Line2D([], [], color="#111", lw=1.3, ls=(0, (3, 2)), label="Center pivot"))
    fig.legend(handles=handles, ncol=min(6, len(handles)), frameon=False, fontsize=7.5,
               loc="lower center", bbox_to_anchor=(0.5, 0.0))
    fig.suptitle(f"Field boundaries coloured by {latest} majority CDL crop", fontweight="bold", y=0.95)
    return _save(fig, path)

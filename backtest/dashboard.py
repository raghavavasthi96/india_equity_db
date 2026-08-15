from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pandas as pd

from .metrics import drawdown_series, TRADING_DAYS

# ----- Dark theme palette ----------------------------------------------------
BG = "#0f1115"
PANEL = "#1a1d24"
ACCENT_GREEN = "#22c55e"
ACCENT_BLUE = "#3b82f6"
ACCENT_GREY = "#8b95a7"
ACCENT_RED = "#ef4444"
ACCENT_AMBER = "#f59e0b"

PLOTLY_TEMPLATE = "plotly_dark"


def _apply_layout(fig, title=None, height=None):
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        paper_bgcolor=PANEL,
        plot_bgcolor=PANEL,
        font=dict(color="#e5e7eb", family="-apple-system, Segoe UI, sans-serif"),
        title=dict(text=title, font=dict(size=14, color="#e5e7eb")) if title else None,
        margin=dict(l=50, r=20, t=50, b=40),
        hoverlabel=dict(bgcolor="#1a1d24", font_color="#e5e7eb"),
    )
    if height:
        fig.update_layout(height=height)
    fig.update_xaxes(gridcolor="#2a2f3a", zerolinecolor="#2a2f3a")
    fig.update_yaxes(gridcolor="#2a2f3a", zerolinecolor="#2a2f3a")
    return fig


def _equity_curve_fig(returns_df):
    import plotly.graph_objects as go
    eq = (1 + returns_df.fillna(0)).cumprod()
    fig = go.Figure()
    for col, color in [("gross", ACCENT_GREEN), ("net", ACCENT_BLUE),
                       ("benchmark", ACCENT_GREY)]:
        if col in eq.columns:
            fig.add_trace(go.Scatter(x=eq.index, y=eq[col], name=col,
                                     line=dict(color=color, width=2)))
    _apply_layout(fig, title="Equity curve (gross / net / benchmark)")
    fig.update_layout(yaxis_title="Cumulative return (x)", hovermode="x unified")
    return fig


def _active_equity_fig(returns_df):
    import plotly.graph_objects as go
    cum_net = (1 + returns_df["net"].fillna(0)).cumprod()
    cum_bench = (1 + returns_df["benchmark"].fillna(0)).cumprod()
    active_cum = cum_net / cum_bench
    fig = go.Figure(go.Scatter(x=active_cum.index, y=active_cum, name="net / bench",
                               line=dict(color=ACCENT_AMBER, width=2)))
    _apply_layout(fig, title="Active equity (net ÷ benchmark)")
    fig.update_layout(hovermode="x unified")
    return fig


def _drawdown_fig(returns_df):
    import plotly.graph_objects as go
    fig = go.Figure()
    for col, color in [("gross", ACCENT_GREEN), ("net", ACCENT_BLUE),
                       ("benchmark", ACCENT_GREY)]:
        if col in returns_df.columns:
            dd = drawdown_series(returns_df[col])
            fig.add_trace(go.Scatter(x=dd.index, y=dd, name=col,
                                     line=dict(color=color, width=2)))
    _apply_layout(fig, title="Drawdown")
    fig.update_layout(yaxis_title="Drawdown", yaxis_tickformat=".0%",
                      hovermode="x unified")
    return fig


def _calendar_bars_fig(returns_df):
    import plotly.graph_objects as go
    years = sorted(returns_df.index.year.unique())
    by_year = {y: returns_df[returns_df.index.year == y] for y in years}
    fig = go.Figure()
    for col, color in [("gross", ACCENT_GREEN), ("net", ACCENT_BLUE),
                       ("benchmark", ACCENT_GREY), ("active", ACCENT_AMBER)]:
        if col not in returns_df.columns:
            continue
        if col == "active":
            vals = [(1 + by_year[y]["net"]).prod() - (1 + by_year[y]["benchmark"]).prod()
                    for y in years]
        else:
            vals = [(1 + by_year[y][col]).prod() - 1 for y in years]
        fig.add_trace(go.Bar(x=years, y=vals, name=col, marker_color=color))
    _apply_layout(fig, title="Calendar-year returns")
    fig.update_layout(barmode="group", yaxis_tickformat=".1%")
    return fig


def _rolling_fig(returns_df, window):
    import plotly.subplots as sp
    import plotly.graph_objects as go
    fig = sp.make_subplots(
        rows=2, cols=2,
        subplot_titles=("Rolling return (ann.)", "Rolling vol (ann.)",
                        "Rolling Sharpe", "Rolling IR"),
        vertical_spacing=0.12, horizontal_spacing=0.08,
    )
    net = returns_df["net"]
    active = returns_df["active"]

    roll_ret = net.rolling(window).apply(
        lambda x: (1 + x).prod() ** (TRADING_DAYS / len(x)) - 1, raw=False
    )
    roll_vol = net.rolling(window).std() * np.sqrt(TRADING_DAYS)
    roll_sharpe = roll_ret / roll_vol.replace(0, np.nan)
    roll_ir = (active.rolling(window).mean() * TRADING_DAYS) / (
        (active.rolling(window).std() * np.sqrt(TRADING_DAYS)).replace(0, np.nan)
    )

    fig.add_trace(go.Scatter(x=roll_ret.index, y=roll_ret, name="ret",
                             line=dict(color=ACCENT_BLUE)), row=1, col=1)
    fig.add_trace(go.Scatter(x=roll_vol.index, y=roll_vol, name="vol",
                             line=dict(color=ACCENT_AMBER)), row=1, col=2)
    fig.add_trace(go.Scatter(x=roll_sharpe.index, y=roll_sharpe, name="sharpe",
                             line=dict(color=ACCENT_GREEN)), row=2, col=1)
    fig.add_trace(go.Scatter(x=roll_ir.index, y=roll_ir, name="IR",
                             line=dict(color=ACCENT_RED)), row=2, col=2)
    _apply_layout(fig, title=f"Rolling stats (window={window} days)", height=720)
    fig.update_layout(showlegend=False)
    for r in (1, 2):
        for c in (1, 2):
            fig.update_xaxes(gridcolor="#2a2f3a", row=r, col=c)
            fig.update_yaxes(gridcolor="#2a2f3a", row=r, col=c)
    return fig


def _bars_fig(df, x_col, y_col, title, y_fmt=None, color=ACCENT_BLUE):
    import plotly.graph_objects as go
    if df is None or df.empty:
        return None
    fig = go.Figure(go.Bar(x=df[x_col], y=df[y_col], marker_color=color))
    _apply_layout(fig, title=title)
    fig.update_layout(hovermode="x unified")
    if y_fmt:
        fig.update_yaxes(tickformat=y_fmt)
    return fig


def _position_count_fig(weights_df):
    import plotly.graph_objects as go
    cnt = (weights_df.drop(columns=["CASH"], errors="ignore") > 1e-9).sum(axis=1)
    fig = go.Figure(go.Scatter(x=cnt.index, y=cnt, name="N positions",
                               line=dict(color=ACCENT_GREEN)))
    _apply_layout(fig, title="Number of positions")
    fig.update_layout(hovermode="x unified")
    return fig


def _top_holdings_fig(weights_df, n=20):
    import plotly.graph_objects as go
    if weights_df.empty:
        return None
    last = weights_df.iloc[-1].drop(labels=["CASH"], errors="ignore")
    top = last[last > 0].nlargest(n)
    fig = go.Figure(go.Bar(x=top.values, y=top.index, orientation="h",
                           marker_color=ACCENT_BLUE))
    _apply_layout(fig, title=f"Top {n} current holdings", height=500)
    fig.update_layout(xaxis_tickformat=".1%", yaxis=dict(autorange="reversed"))
    return fig


def _active_weights_fig(weights_df, ctx, n=10):
    import plotly.graph_objects as go
    if weights_df.empty:
        return None
    from .context import benchmark_weights_asof
    last_date = weights_df.index[-1]
    port_w = weights_df.iloc[-1].drop(labels=["CASH"], errors="ignore")
    # Same helper the sector tilts use, so stock-level and sector-level active
    # weights can never be computed off different benchmark vectors.
    bench_w = benchmark_weights_asof(ctx, last_date)
    if bench_w.empty:
        return None
    all_syms = port_w.index.union(bench_w.index)
    diff = port_w.reindex(all_syms).fillna(0) - bench_w.reindex(all_syms).fillna(0)
    diff = diff[diff != 0]
    over = diff.nlargest(n)
    under = diff.nsmallest(n)
    combined = pd.concat([over, under])
    colors = [ACCENT_GREEN] * len(over) + [ACCENT_RED] * len(under)
    fig = go.Figure(go.Bar(x=combined.values, y=combined.index, orientation="h",
                           marker_color=colors))
    _apply_layout(fig, title=f"Top {n} active over/under-weights vs benchmark",
                  height=600)
    fig.update_layout(xaxis_tickformat=".2%", yaxis=dict(autorange="reversed"))
    return fig


def _concentration_fig(weights_df):
    import plotly.graph_objects as go
    if weights_df.empty:
        return None
    last = weights_df.iloc[-1].drop(labels=["CASH"], errors="ignore")
    last = last[last > 0].sort_values(ascending=False)
    cum = last.cumsum().values
    hhi = float((last ** 2).sum())
    fig = go.Figure(go.Scatter(x=list(range(1, len(cum) + 1)), y=cum,
                               mode="lines+markers",
                               line=dict(color=ACCENT_BLUE)))
    _apply_layout(fig, title=f"Cumulative weight concentration (HHI={hhi:.4f})")
    fig.update_layout(xaxis_title="Holdings (sorted by weight, desc)",
                      yaxis_title="Cumulative weight",
                      yaxis_tickformat=".0%")
    return fig


# ----- Sector / size breakdown over time -------------------------------------

def _build_bucket_breakdown(weights_df, ctx, classifier_fn, bucket_names):
    """For each day, sum weights per bucket. classifier_fn(snap) -> {symbol: bucket}.

    Uses a *cumulative* classifier: walking snaps chronologically, each snap's
    classifications overwrite/extend the running map. Symbols that drop out of
    the universe keep their last-known bucket; rank changes reclassify on the
    next snap. This avoids spurious "unclassified" weight on days where the
    portfolio holds names that aren't in the currently-active snapshot
    (quarter-end-day pre-trade window + post-rebal universe exits).
    """
    from collections import defaultdict

    snap_dates = ctx.rebal_quarter_ends
    if len(snap_dates) == 0 or weights_df.empty:
        return pd.DataFrame()

    # Build cumulative classifier snapshot: snap_d -> {symbol: bucket} as known by snap_d.
    cumulative_at: dict = {}
    cumulative: dict = {}
    for qed in snap_dates:
        snap = ctx.universe_by_date[qed]
        cumulative.update(classifier_fn(snap))
        cumulative_at[qed] = dict(cumulative)

    # Active snap date per row
    snap_per_day = pd.Series(index=weights_df.index, dtype="datetime64[ns]")
    for d in weights_df.index:
        valid = snap_dates[snap_dates <= d]
        snap_per_day.loc[d] = valid[-1] if len(valid) else pd.NaT

    cols = list(bucket_names) + ["CASH", "unclassified"]
    out = pd.DataFrame(0.0, index=weights_df.index, columns=cols)

    for snap_d, days in snap_per_day.groupby(snap_per_day).groups.items():
        if pd.isna(snap_d):
            continue
        sym_to_bucket = cumulative_at[snap_d]
        sub = weights_df.loc[days]

        if "CASH" in sub.columns:
            out.loc[days, "CASH"] = sub["CASH"].values

        bucket_syms = defaultdict(list)
        for sym in sub.columns:
            if sym == "CASH":
                continue
            b = sym_to_bucket.get(sym, "unclassified")
            bucket_syms[b].append(sym)

        for bucket, syms in bucket_syms.items():
            if bucket not in out.columns:
                out[bucket] = 0.0
            out.loc[days, bucket] = sub[syms].sum(axis=1).values

    # Drop columns that are always 0
    out = out.loc[:, (out.abs().sum() > 1e-9)]
    return out


def _size_breakdown(weights_df, ctx):
    size_buckets = ctx.cfg.size_buckets

    def classify(snap):
        ranks = snap["rank"].astype(int)
        m = {}
        for sym, rk in ranks.items():
            for name, (lo, hi) in size_buckets.items():
                hi_val = hi if hi is not None else 10 ** 9
                if lo <= rk <= hi_val:
                    m[sym] = name
                    break
        return m

    return _build_bucket_breakdown(weights_df, ctx, classify, list(size_buckets.keys()))


def _sector_breakdown(weights_df, ctx):
    if ctx.sector_map is None:
        return None

    def classify(snap):
        return {sym: ctx.sector_map.get(sym, "UNKNOWN") for sym in snap.index}

    # Build with empty bucket_names; collect dynamically inside
    return _build_bucket_breakdown(weights_df, ctx, classify, [])


def _sector_contribution_fig(attribution):
    """Cumulative net return contribution by sector, in % of starting NAV."""
    import plotly.graph_objects as go
    if attribution is None or attribution.empty:
        return None
    end = attribution["date"].max()
    vals = (attribution[attribution["date"] == end]
            .set_index("sector")["cum_contrib_net_pct"])
    vals = vals[vals.abs() > 1e-9].sort_values()
    if vals.empty:
        return None
    colors = [ACCENT_GREEN if v >= 0 else ACCENT_RED for v in vals.values]
    fig = go.Figure(go.Bar(x=vals.values, y=vals.index, orientation="h",
                           marker_color=colors))
    _apply_layout(fig, title="Cumulative net contribution by sector (% of starting NAV)",
                  height=max(320, 22 * len(vals) + 140))
    fig.update_layout(xaxis_ticksuffix="%")
    return fig


def _sector_tilt_fig(active):
    """End-of-run active sector weight vs the ff-mcap top-500 weight benchmark."""
    import plotly.graph_objects as go
    if active is None or active.empty:
        return None
    end = active["date"].max()
    vals = active[active["date"] == end].set_index("sector")["active_wt"]
    vals = vals[vals.abs() > 1e-9].sort_values()
    if vals.empty:
        return None
    colors = [ACCENT_GREEN if v >= 0 else ACCENT_RED for v in vals.values]
    fig = go.Figure(go.Bar(x=vals.values, y=vals.index, orientation="h",
                           marker_color=colors))
    # The return benchmark is the Nifty 500 TRI, but the *weight* benchmark can
    # only be the PIT ff-mcap universe (the TRI file has no constituents), so the
    # title has to name which one this is.
    _apply_layout(fig, title="Active sector weight vs ff-mcap top-500 (latest)",
                  height=max(320, 22 * len(vals) + 140))
    fig.update_layout(xaxis_tickformat=".2%")
    return fig


def _area_fig(df, title, palette=None):
    import plotly.graph_objects as go
    if df is None or df.empty:
        return None
    fig = go.Figure()
    # Sort columns by mean weight desc for stable ordering
    col_order = df.mean().sort_values(ascending=False).index.tolist()
    default_palette = [
        ACCENT_BLUE, ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED, ACCENT_GREY,
        "#a855f7", "#06b6d4", "#ec4899", "#10b981", "#f97316",
        "#6366f1", "#84cc16", "#eab308", "#14b8a6", "#f43f5e",
    ]
    palette = palette or default_palette
    for i, col in enumerate(col_order):
        color = palette[i % len(palette)]
        fig.add_trace(go.Scatter(
            x=df.index, y=df[col], name=str(col),
            mode="lines", stackgroup="one",
            line=dict(width=0.5, color=color),
            fillcolor=color, hovertemplate="%{y:.2%}<extra>%{fullData.name}</extra>",
        ))
    _apply_layout(fig, title=title, height=440)
    fig.update_layout(yaxis_tickformat=".0%", hovermode="x unified",
                      yaxis=dict(range=[0, 1]))
    return fig


# ----- Stats tables ----------------------------------------------------------

def _fmt_stat(key, val):
    if val is None:
        return "—"
    if isinstance(val, str):
        return val
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, (int, np.integer)):
        return f"{int(val):,}"
    if isinstance(val, float):
        if "drawdown" in key and "recovery" not in key and "start" not in key and "end" not in key:
            return f"{val:.2%}"
        if any(t in key for t in ["cagr", "vol", "tracking", "turnover", "hit_rate"]):
            return f"{val:.2%}"
        if "tcost" in key:
            return f"{val:.1f}"
        if "avg_n_positions" == key:
            return f"{val:.0f}"
        return f"{val:.3f}"
    return str(val)


def _stat_card(title, items, summary):
    rows = []
    for label, key in items:
        v = summary.get(key)
        rows.append(
            f"<tr><td class='lbl'>{label}</td>"
            f"<td class='val'>{_fmt_stat(key, v)}</td></tr>"
        )
    return (
        f"<div class='stat-card'>"
        f"<h3>{title}</h3>"
        f"<table>{''.join(rows)}</table>"
        f"</div>"
    )


def _stats_section_html(summary):
    returns_items = [
        ("Gross CAGR", "cagr_gross"),
        ("Net CAGR", "cagr_net"),
        ("Benchmark CAGR", "cagr_benchmark"),
    ]
    risk_items = [
        ("Net vol", "vol_net"),
        ("Benchmark vol", "vol_benchmark"),
        ("Net Sharpe", "sharpe_net"),
        ("Benchmark Sharpe", "sharpe_benchmark"),
    ]
    active_items = [
        ("Tracking error", "tracking_error"),
        ("Information ratio", "information_ratio"),
        ("Hit rate (active)", "hit_rate_active"),
    ]
    dd_items = [
        ("Net max DD", "net_max_drawdown"),
        ("Net DD start", "net_max_drawdown_start"),
        ("Net DD end", "net_max_drawdown_end"),
        ("Net DD recov. (d)", "net_max_drawdown_recovery_days"),
    ]
    portfolio_items = [
        ("Avg # positions", "avg_n_positions"),
        ("Turnover / rebal", "turnover_per_rebal"),
        ("Turnover annualised", "turnover_annualized"),
        ("T-cost bps / rebal", "tcost_bps_per_rebal"),
        ("T-cost bps annualised", "tcost_bps_annualized"),
    ]
    cards = "".join([
        _stat_card("Returns", returns_items, summary),
        _stat_card("Risk", risk_items, summary),
        _stat_card("Active", active_items, summary),
        _stat_card("Drawdown", dd_items, summary),
        _stat_card("Portfolio", portfolio_items, summary),
    ])
    return f"<div class='stat-grid'>{cards}</div>"


def _header_html(cfg):
    rows = [
        ("Run ID", cfg.run_id or "—"),
        ("Window", f"{cfg.start or 'full'} → {cfg.end or 'full'}"),
        ("Rebal freq", cfg.rebal_freq),
        ("Weighting", cfg.weighting),
        ("Signal", cfg.signal_name or (
            getattr(cfg.signal_fn, "__name__", None) or "—"
        )),
        ("Gamma", f"{cfg.tilt_gamma:g}" if cfg.tilt_gamma is not None else "—"),
        ("Signal top N", f"{int(cfg.signal_top_n)}" if cfg.signal_top_n is not None else "—"),
        ("Signal top quantile",
         f"{cfg.signal_top_quantile:.0%}" if cfg.signal_top_quantile is not None else "—"),
        ("Max stock wt", f"{cfg.max_stock_wt:.0%}" if cfg.max_stock_wt else "—"),
        (f"Max {cfg.sector_level} wt",
         f"{cfg.max_sector_wt:.0%}" if cfg.max_sector_wt is not None else "—"),
        ("Cash buffer", f"{cfg.cash_buffer:.0%}"),
        ("Benchmark", cfg.benchmark),
    ]
    cells = "".join(
        f"<div class='hcell'><div class='hk'>{k}</div>"
        f"<div class='hv'>{v}</div></div>"
        for k, v in rows
    )
    return f"<div class='header'>{cells}</div>"


def render_dashboard(result, out_path: str) -> str:
    import plotly.io as pio

    cfg = result.cfg
    ctx = result.ctx
    summary = result.summary

    eq_fig = _equity_curve_fig(result.returns)
    active_eq_fig = _active_equity_fig(result.returns)
    dd_fig = _drawdown_fig(result.returns)
    cal_fig = _calendar_bars_fig(result.returns)
    rolling_fig = _rolling_fig(result.returns, cfg.rolling_window_days)

    turn_fig = _bars_fig(result.turnover, "trade_date", "one_way_turnover",
                          "One-way turnover per rebalance", ".0%", ACCENT_AMBER)
    tcost_fig = _bars_fig(result.tcost, "trade_date", "drag_bps",
                           "T-cost drag per rebalance (bps)", None, ACCENT_RED)
    pos_fig = _position_count_fig(result.weights)
    top_fig = _top_holdings_fig(result.weights)
    active_w_fig = _active_weights_fig(result.weights, ctx)
    conc_fig = _concentration_fig(result.weights)

    size_df = _size_breakdown(result.weights, ctx)
    sector_df = _sector_breakdown(result.weights, ctx)
    size_fig = _area_fig(size_df, "Size-bucket weight over time")
    sector_fig = (_area_fig(sector_df, f"{cfg.sector_level} weight over time")
                  if sector_df is not None else None)

    from .attribution import build_sector_frames
    attribution, active = build_sector_frames(result)
    sector_contrib_fig = _sector_contribution_fig(attribution)
    sector_tilt_fig = _sector_tilt_fig(active)

    def _h(fig, div_id):
        if fig is None:
            return ""
        return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                           div_id=div_id)

    eq_with_toggle = (
        "<div class='chart-toggle'>"
        "<button class='active' onclick=\"toggleYAxis(this,'fig_eq','linear')\">Linear</button>"
        "<button onclick=\"toggleYAxis(this,'fig_eq','log')\">Log</button>"
        "</div>" + _h(eq_fig, "fig_eq")
    )
    perf_html = (eq_with_toggle + _h(active_eq_fig, "fig_active")
                  + _h(dd_fig, "fig_dd") + _h(cal_fig, "fig_cal"))

    risk_html = _h(rolling_fig, "fig_roll") + _h(turn_fig, "fig_turn") + \
                 _h(tcost_fig, "fig_tcost") + _h(pos_fig, "fig_pos")

    comp_html = _h(top_fig, "fig_top") + _h(active_w_fig, "fig_act") + _h(conc_fig, "fig_conc")

    breakdown_html = _h(size_fig, "fig_size")
    if sector_fig is not None:
        breakdown_html += (_h(sector_fig, "fig_sector")
                           + _h(sector_contrib_fig, "fig_sector_contrib")
                           + _h(sector_tilt_fig, "fig_sector_tilt"))
    else:
        breakdown_html += (
            "<div class='note'>Sector breakdown unavailable — no sector map found. "
            "Run <code>python -m pipeline.fetch_sectors</code>, or point "
            "<code>sector_map_csv</code> at a classification CSV.</div>"
        )

    plotly_cdn = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Backtest — {cfg.run_id or 'run'}</title>
{plotly_cdn}
<style>
  :root {{
    --bg: {BG};
    --panel: {PANEL};
    --panel-2: #20242d;
    --border: #2a2f3a;
    --text: #e5e7eb;
    --text-dim: #9ca3af;
    --text-dimmer: #6b7280;
  }}
  html, body {{ background: var(--bg); color: var(--text); margin: 0;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                             sans-serif; }}
  body {{ padding: 24px 32px; }}
  h1 {{ font-size: 22px; margin: 0 0 16px 0; color: var(--text); }}
  h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: 0.06em;
        color: var(--text-dim); margin: 36px 0 12px 0;
        border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  .header {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }}
  .hcell {{ background: var(--panel); padding: 8px 14px; border-radius: 6px;
            min-width: 140px; border: 1px solid var(--border); }}
  .hk {{ font-size: 10px; color: var(--text-dim); text-transform: uppercase;
         letter-spacing: 0.06em; }}
  .hv {{ font-size: 14px; font-weight: 600; margin-top: 3px; color: var(--text); }}

  .stat-grid {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }}
  .stat-card {{ background: var(--panel); border: 1px solid var(--border);
                 border-radius: 8px; padding: 12px 16px; min-width: 280px; flex: 0 1 auto; }}
  .stat-card h3 {{ margin: 0 0 8px 0; font-size: 11px; color: var(--text-dim);
                    text-transform: uppercase; letter-spacing: 0.08em; }}
  .stat-card table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .stat-card td {{ padding: 5px 0; border-bottom: 1px solid var(--border); }}
  .stat-card td:last-child {{ text-align: right; }}
  .stat-card tr:last-child td {{ border-bottom: none; }}
  .stat-card td.lbl {{ color: var(--text-dim); }}
  .stat-card td.val {{ font-family: "SF Mono", Consolas, monospace;
                        color: var(--text); font-weight: 500; }}

  .note {{ background: var(--panel); border: 1px dashed var(--border);
            border-radius: 6px; padding: 14px; color: var(--text-dim);
            font-size: 13px; }}
  .note code {{ background: var(--panel-2); padding: 2px 6px; border-radius: 3px;
                 color: var(--text); }}

  .footer {{ margin-top: 40px; font-size: 11px; color: var(--text-dimmer);
              border-top: 1px solid var(--border); padding-top: 12px; }}

  /* Plotly figure cards */
  .js-plotly-plot {{ background: var(--panel); border-radius: 8px;
                      border: 1px solid var(--border); margin-bottom: 14px; }}

  /* Custom yaxis toggle buttons */
  .chart-toggle {{ display: flex; gap: 6px; justify-content: flex-end;
                    margin: 0 0 6px 0; }}
  .chart-toggle button {{ background: var(--panel); border: 1px solid var(--border);
                            color: var(--text-dim); padding: 4px 12px;
                            border-radius: 4px; cursor: pointer; font-size: 12px;
                            font-family: inherit; }}
  .chart-toggle button:hover {{ background: var(--panel-2); color: var(--text); }}
  .chart-toggle button.active {{ background: {ACCENT_BLUE}; color: #ffffff;
                                   border-color: {ACCENT_BLUE}; }}
</style>
<script>
function toggleYAxis(btn, divId, axisType) {{
  var siblings = btn.parentElement.querySelectorAll('button');
  siblings.forEach(function(b) {{ b.classList.remove('active'); }});
  btn.classList.add('active');
  Plotly.relayout(divId, {{'yaxis.type': axisType}});
}}
</script>
</head>
<body>
<h1>Backtest — {cfg.run_id or 'run'}</h1>
{_header_html(cfg)}

<h2>Performance</h2>
{perf_html}

<h2>Stats</h2>
{_stats_section_html(summary)}

<h2>Risk & turnover</h2>
{risk_html}

<h2>Sector & size breakdown</h2>
{breakdown_html}

<h2>Composition (latest)</h2>
{comp_html}

<div class="footer">Generated {datetime.utcnow().isoformat()}Z ·
weighting={cfg.weighting} · rebal={cfg.rebal_freq} ·
n_days={len(result.returns)}</div>

</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path

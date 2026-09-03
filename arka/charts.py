"""Plotly figure builders.

Rendering only. Every figure takes numbers that have already been computed —
nothing here calculates a displayed quantity, and nothing here reads the
benchmark CSV. Rounding for presentation happens at this layer, never in the
model (CLAUDE.md rule 5).

No Streamlit import: these are plain plotly figures the shell hands to
`st.plotly_chart`.
"""

from __future__ import annotations

from typing import Any, Sequence

import plotly.graph_objects as go

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_MODULE_FILL = "#2a4d7a"
_MODULE_EDGE = "#16304d"
_AREA_FILL = "#cfd8e3"

_LAYOUT = dict(
    margin=dict(l=50, r=20, t=50, b=40),
    template="plotly_white",
    hoverlabel=dict(namelength=-1),
)


def monthly_generation(monthly_kwh: Sequence[float], title: str = "Monthly generation") -> go.Figure:
    """Twelve bars of monthly output."""
    figure = go.Figure(
        go.Bar(
            x=list(MONTHS),
            y=[round(v) for v in monthly_kwh],
            hovertemplate="%{x}: %{y:,.0f} kWh<extra></extra>",
        )
    )
    figure.update_layout(title=title, yaxis_title="kWh", **_LAYOUT)
    return figure


def hourly_heatmap(grid: Sequence[Sequence[float]], title: str = "Hourly generation") -> go.Figure:
    """24 x 365 heatmap. `grid` comes from `resource.hourly_to_heatmap`."""
    figure = go.Figure(
        go.Heatmap(
            z=grid,
            x=list(range(1, 366)),
            y=list(range(24)),
            colorbar=dict(title="kWh"),
            hovertemplate="day %{x}, hour %{y}: %{z:,.1f} kWh<extra></extra>",
        )
    )
    figure.update_layout(
        title=title, xaxis_title="Day of year", yaxis_title="Hour of day", **_LAYOUT
    )
    return figure


def load_overlay(
    generation_by_hour: Sequence[float],
    load_by_hour: Sequence[float],
    title: str = "Average day",
) -> go.Figure:
    """Generation against load across an average 24 hours."""
    hours = list(range(24))
    figure = go.Figure()
    figure.add_trace(go.Scatter(x=hours, y=generation_by_hour, name="Generation", fill="tozeroy"))
    figure.add_trace(go.Scatter(x=hours, y=load_by_hour, name="Load", mode="lines"))
    figure.update_layout(
        title=title, xaxis_title="Hour of day", yaxis_title="kWh", **_LAYOUT
    )
    return figure


def self_consumption_curve(
    sizes_kwh: Sequence[float],
    fractions: Sequence[float],
    title: str = "Self-consumption against battery size",
) -> go.Figure:
    """The diminishing-returns curve that sets battery size."""
    figure = go.Figure(
        go.Scatter(
            x=list(sizes_kwh),
            y=[f * 100.0 for f in fractions],
            mode="lines+markers",
            hovertemplate="%{x:,.0f} kWh: %{y:.1f}%<extra></extra>",
        )
    )
    figure.update_layout(
        title=title, xaxis_title="Usable battery capacity (kWh)",
        yaxis_title="Self-consumption (%)", **_LAYOUT,
    )
    return figure


def cashflow_waterfall(cashflows: Sequence[float], currency: str = "GBP") -> go.Figure:
    """Year 0 capex and the annual net benefits that follow."""
    labels = ["Capex"] + [f"Y{i}" for i in range(1, len(cashflows))]
    figure = go.Figure(
        go.Waterfall(
            x=labels,
            y=[round(v, 2) for v in cashflows],
            measure=["relative"] * len(cashflows),
            connector=dict(line=dict(width=1)),
            hovertemplate="%{x}: %{y:,.0f} " + currency + "<extra></extra>",
        )
    )
    figure.update_layout(title="Cashflow", yaxis_title=currency, **_LAYOUT)
    return figure


def cumulative_discounted(
    discounted: Sequence[float],
    currency: str = "GBP",
    payback_years: float | None = None,
) -> go.Figure:
    """Cumulative discounted cashflow, with the payback crossing marked."""
    running: list[float] = []
    total = 0.0
    for value in discounted:
        total += value
        running.append(total)
    figure = go.Figure(
        go.Scatter(
            x=list(range(len(running))),
            y=running,
            mode="lines+markers",
            name="Cumulative discounted cashflow",
            hovertemplate="Year %{x}: %{y:,.0f} " + currency + "<extra></extra>",
        )
    )
    figure.add_hline(y=0.0, line_width=1, line_dash="dot")
    if payback_years is not None:
        figure.add_vline(
            x=payback_years, line_width=1, line_dash="dash",
            annotation_text=f"payback {payback_years:.1f} yr", annotation_position="top",
        )
    figure.update_layout(
        title="Cumulative discounted cashflow", xaxis_title="Year",
        yaxis_title=currency, **_LAYOUT,
    )
    return figure


def macc(bars: Sequence[dict[str, Any]], currency: str = "GBP") -> go.Figure:
    """Marginal abatement cost curve.

    Bars arrive from `finance.macc_curve` already additive and laid out: each
    bar's width is its own abatement and it starts where the previous one ends.
    Widths and positions are honoured exactly — this function does no arithmetic
    beyond centring each bar on its own span.
    """
    figure = go.Figure()
    for bar in bars:
        width = float(bar["width"])
        figure.add_trace(
            go.Bar(
                x=[float(bar["x_start"]) + width / 2.0],
                y=[float(bar["cost_per_tco2"])],
                width=[width],
                name=str(bar["label"]),
                hovertemplate=(
                    f"<b>{bar['label']}</b><br>"
                    f"{float(bar['delta_tco2']):,.0f} tCO2e<br>"
                    f"{float(bar['delta_capex']):,.0f} {currency}<br>"
                    "%{y:,.0f} " + f"{currency}/tCO2e<extra></extra>"
                ),
            )
        )
    figure.add_hline(y=0.0, line_width=1)
    figure.update_layout(
        title="Marginal abatement cost curve",
        xaxis_title="Cumulative abatement (tCO2e)",
        yaxis_title=f"{currency} per tCO2e",
        barmode="overlay", bargap=0.0, **_LAYOUT,
    )
    return figure


def layout_plan(
    polygon_m: Sequence[tuple[float, float]],
    holes_m: Sequence[Sequence[tuple[float, float]]],
    module_outlines: Sequence[Sequence[tuple[float, float]]],
    title: str = "Packing preview",
) -> go.Figure:
    """Plan view of the packed array in the site-local metric frame."""
    figure = go.Figure()
    if polygon_m:
        figure.add_trace(
            go.Scatter(
                x=[p[0] for p in polygon_m], y=[p[1] for p in polygon_m],
                mode="lines", name="Usable area", fill="toself", opacity=0.35,
                line=dict(color="#8a94a6", width=1), fillcolor=_AREA_FILL,
            )
        )
    for hole in holes_m:
        figure.add_trace(
            go.Scatter(
                x=[p[0] for p in hole], y=[p[1] for p in hole],
                mode="lines", name="Exclusion", fill="toself", opacity=0.4,
                showlegend=False,
            )
        )
    for index, outline in enumerate(module_outlines):
        closed = list(outline) + [outline[0]]
        figure.add_trace(
            go.Scatter(
                x=[p[0] for p in closed], y=[p[1] for p in closed],
                mode="lines", fill="toself",
                # One fixed colour: every module is the same object, so letting
                # plotly cycle its palette per trace would be noise, not meaning.
                line=dict(width=0.5, color=_MODULE_EDGE),
                fillcolor=_MODULE_FILL,
                name="Module", legendgroup="modules",
                showlegend=index == 0, hoverinfo="skip",
            )
        )
    figure.update_layout(title=title, xaxis_title="metres east", yaxis_title="metres north", **_LAYOUT)
    figure.update_yaxes(scaleanchor="x", scaleratio=1.0)
    return figure

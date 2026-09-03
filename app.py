"""Arka — Streamlit shell.

Routing and rendering only. Every figure on every screen comes from `arka.*`;
this file computes nothing it displays. Rounding happens here, at the
presentation layer, and nowhere else.
"""

from __future__ import annotations

import json
from dataclasses import replace

import streamlit as st
from streamlit.components.v1 import html as render_html

from arka import benchmarks, charts, dispatch, finance, geometry, modules, resource
from arka.agent import client as agent_client
from arka.scenario import (
    ArraySpec,
    ArrayType,
    BatterySpec,
    FinanceInputs,
    Market,
    Mounting,
    Orientation,
    Scenario,
    Site,
)

SCREENS = ("Site", "Array", "Yield", "Storage", "Finance", "Report")

TILES = {
    "Satellite (Esri World Imagery)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Esri, Maxar, Earthstar Geographics",
    ),
    "OpenStreetMap": ("OpenStreetMap", "OpenStreetMap contributors"),
}

# Where each market's map opens before a site is drawn.
MARKET_HOME = {
    Market.UK: (51.5074, -0.1278, 11),
    Market.INDIA: (19.0760, 72.8777, 11),
    Market.UAE: (25.2048, 55.2708, 11),
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def scenario() -> Scenario:
    if "scenario" not in st.session_state:
        st.session_state.scenario = Scenario()
    return st.session_state.scenario


def table() -> benchmarks.BenchmarkTable:
    return benchmarks.load()


def frame_and_polygon(sc: Scenario):
    """Local frame, boundary polygon and exclusion polygons, or (None, None, [])."""
    if len(sc.site.boundary) < 3:
        return None, None, []
    frame = geometry.LocalFrame.for_site(sc.site)
    boundary = geometry.ring_to_polygon(sc.site.boundary, frame)
    holes = [geometry.ring_to_polygon(ring, frame) for ring in sc.site.exclusions if len(ring) >= 3]
    return frame, boundary, holes


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------


def assumptions_panel(sc: Scenario, key: str) -> None:
    """Rule 4: every output screen can show what fed it, with source tiers."""
    with st.expander("Assumptions and sources", expanded=False):
        rows = table().assumptions(sc.site.market.value)
        st.caption(
            "Tier is source quality. `gap` rows are empty on purpose — they have no "
            "defensible open source and must be supplied by you."
        )
        st.dataframe(rows, use_container_width=True, hide_index=True, key=f"assumptions_{key}")


def gap_warning(sc: Scenario) -> None:
    gaps = table().for_market(sc.site.market.value).gaps()
    if gaps:
        names = ", ".join(row.parameter for row in gaps)
        st.info(f"Inputs you must supply for {sc.site.market.value}: {names}")


def metric_row(items: list[tuple[str, str, str | None]]) -> None:
    for column, (label, value, help_text) in zip(st.columns(len(items)), items):
        column.metric(label, value, help=help_text)


# ---------------------------------------------------------------------------
# 1. Site
# ---------------------------------------------------------------------------


def screen_site(sc: Scenario) -> None:
    st.header("Site")
    st.caption("Draw the array boundary first, then any exclusion zones inside it.")

    left, right = st.columns([2, 1])

    with right:
        sc.site = replace(sc.site, name=st.text_input("Site name", sc.site.name))
        market = st.selectbox(
            "Market", list(Market), index=list(Market).index(sc.site.market),
            format_func=lambda m: m.value,
        )
        if market is not sc.site.market:
            sc.site = replace(sc.site, market=market)
            sc.invalidate_from("site")

        array_type = st.radio(
            "Array type", list(ArrayType), index=list(ArrayType).index(sc.array.array_type),
            format_func=lambda a: a.value.replace("_", " ").title(),
        )
        if array_type is not sc.array.array_type:
            sc.array = replace(sc.array, array_type=array_type)
            sc.invalidate_from("array")
        if array_type is not ArrayType.ROOFTOP:
            st.warning(
                f"{array_type.value.replace('_', ' ').title()} is interface only. "
                "The packing routine will raise rather than return a rooftop answer "
                "dressed up as something else."
            )

        basemap = st.radio("Basemap", list(TILES), index=0)

        if sc.site.boundary:
            st.success(f"Boundary captured: {len(sc.site.boundary)} vertices")
            if st.button("Clear site geometry"):
                sc.site = replace(sc.site, boundary=[], exclusions=[])
                sc.invalidate_from("site")
                st.rerun()

    with left:
        _draw_map(sc, basemap)

    _geojson_fallback(sc)

    if sc.site.boundary:
        frame, boundary, holes = frame_and_polygon(sc)
        usable = geometry.usable_area(boundary, holes, edge_setback_m=sc.array.edge_setback_m)
        implied = geometry.azimuth_from_longest_edge(boundary, frame.origin_lat)
        metric_row(
            [
                ("Gross area", f"{geometry.area_m2(boundary):,.0f} m²", "Polygon as drawn"),
                ("Usable area", f"{geometry.area_m2(usable):,.0f} m²",
                 f"After a {sc.array.edge_setback_m:g} m setback and {len(holes)} exclusion zone(s)"),
                ("Implied azimuth", f"{implied:.0f}°",
                 "Normal to the longest edge, taking the equator-facing option"),
            ]
        )
    else:
        st.info("No boundary yet. Draw a polygon on the map, or paste GeoJSON below.")

    assumptions_panel(sc, "site")


def _draw_map(sc: Scenario, basemap: str) -> None:
    try:
        import folium
        from folium.plugins import Draw
        from streamlit_folium import st_folium
    except ImportError:
        st.error("streamlit-folium is not installed; run `pip install -r requirements.txt`.")
        return

    centre = sc.site.centre
    if centre is not None:
        lat, lon, zoom = centre[1], centre[0], 18
    else:
        lat, lon, zoom = MARKET_HOME[sc.site.market]

    tiles, attribution = TILES[basemap]
    fmap = folium.Map(location=[lat, lon], zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(tiles=tiles, attr=attribution, name=basemap, max_zoom=21).add_to(fmap)
    Draw(
        export=False,
        draw_options={
            "polyline": False, "circle": False, "circlemarker": False, "marker": False,
            "rectangle": True, "polygon": True,
        },
    ).add_to(fmap)

    if sc.site.boundary:
        folium.Polygon(
            locations=[(lat_, lon_) for lon_, lat_ in sc.site.boundary],
            color="#ffcc00", weight=2, fill=True, fill_opacity=0.15, tooltip="Array boundary",
        ).add_to(fmap)
    for ring in sc.site.exclusions:
        folium.Polygon(
            locations=[(lat_, lon_) for lon_, lat_ in ring],
            color="#ff4b4b", weight=2, fill=True, fill_opacity=0.35, tooltip="Exclusion",
        ).add_to(fmap)

    state = st_folium(fmap, height=520, use_container_width=True, key="site_map")
    drawings = (state or {}).get("all_drawings") or []
    rings = [
        [(float(x), float(y)) for x, y in feature["geometry"]["coordinates"][0]]
        for feature in drawings
        if feature.get("geometry", {}).get("type") == "Polygon"
    ]
    if rings and rings != [sc.site.boundary, *sc.site.exclusions]:
        if st.button(f"Use these {len(rings)} shape(s): first as boundary, rest as exclusions"):
            sc.site = replace(sc.site, boundary=rings[0], exclusions=rings[1:])
            sc.invalidate_from("site")
            st.rerun()


def _geojson_fallback(sc: Scenario) -> None:
    with st.expander("Paste GeoJSON instead"):
        st.caption("A Polygon, or a FeatureCollection whose first polygon is the boundary.")
        text = st.text_area("GeoJSON", height=120, key="geojson_in")
        if st.button("Load GeoJSON") and text.strip():
            try:
                rings = _rings_from_geojson(json.loads(text))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                st.error(f"Could not read that GeoJSON: {exc}")
                return
            if not rings:
                st.error("No polygons found in that GeoJSON.")
                return
            sc.site = replace(sc.site, boundary=rings[0], exclusions=rings[1:])
            sc.invalidate_from("site")
            st.rerun()


def _rings_from_geojson(payload: dict) -> list[list[tuple[float, float]]]:
    features = payload.get("features") or [payload]
    rings: list[list[tuple[float, float]]] = []
    for feature in features:
        geom = feature.get("geometry", feature)
        if geom.get("type") == "Polygon":
            for index, ring in enumerate(geom["coordinates"]):
                rings.append([(float(x), float(y)) for x, y in ring])
                if index == 0 and len(geom["coordinates"]) == 1:
                    break
        elif geom.get("type") == "MultiPolygon":
            for polygon in geom["coordinates"]:
                rings.append([(float(x), float(y)) for x, y in polygon[0]])
    return rings


# ---------------------------------------------------------------------------
# 2. Array
# ---------------------------------------------------------------------------


def screen_array(sc: Scenario) -> None:
    st.header("Array")
    if len(sc.site.boundary) < 3:
        st.warning("Draw a site boundary first.")
        return

    left, right = st.columns([1, 2])

    with left:
        _module_picker(sc)
        mounting = st.radio(
            "Mounting", list(Mounting), index=list(Mounting).index(sc.array.mounting),
            format_func=lambda m: "Flush to the roof pitch" if m is Mounting.FLUSH
            else "Ballasted on a flat roof",
        )
        orientation = st.radio(
            "Orientation", list(Orientation),
            index=list(Orientation).index(sc.array.orientation),
            format_func=lambda o: o.value.title(), horizontal=True,
        )
        tilt = st.slider("Tilt (degrees)", 0.0, 45.0, float(sc.array.tilt_deg), 1.0)

        frame, boundary, holes = frame_and_polygon(sc)
        implied = geometry.azimuth_from_longest_edge(boundary, frame.origin_lat)
        use_implied = st.checkbox("Take azimuth from the longest edge", value=sc.array.azimuth_deg is None)
        azimuth = None if use_implied else st.slider(
            "Azimuth (degrees clockwise from north)", 0.0, 359.0,
            float(sc.array.azimuth_deg if sc.array.azimuth_deg is not None else implied), 1.0,
        )

        setback = st.slider("Edge setback (m)", 0.0, 3.0, float(sc.array.edge_setback_m), 0.1)
        altitude = st.slider(
            "Minimum solar altitude for row spacing (degrees)", 5.0, 45.0,
            float(sc.array.min_solar_altitude_deg), 1.0,
            help="Rows are spaced so the row in front does not shade the row behind above this "
                 "sun angle. Only applies to ballasted mounting.",
            disabled=mounting is Mounting.FLUSH,
        )

        sc.array = replace(
            sc.array, mounting=mounting, orientation=orientation, tilt_deg=tilt,
            azimuth_deg=azimuth, edge_setback_m=setback, min_solar_altitude_deg=altitude,
        )

    with right:
        if sc.array.module is None:
            st.info("Pick a module to see the packing preview.")
            return
        try:
            layout = geometry.pack(
                boundary, sc.array.array_type, sc.array, latitude=frame.origin_lat, exclusions=holes
            )
        except NotImplementedError as exc:
            st.error(str(exc))
            return
        except geometry.PackingError as exc:
            st.error(f"Cannot pack this array: {exc}")
            return

        if layout != sc.layout:
            sc.layout = layout
            sc.invalidate_from("yield")

        metric_row(
            [
                ("Modules", f"{layout.module_count:,}", f"{layout.rows} rows"),
                ("Capacity", f"{layout.kwp:,.1f} kWp", None),
                ("Row pitch", f"{layout.row_pitch_m:.2f} m", "Centre to centre, in plan"),
                ("Azimuth", f"{layout.azimuth_deg:.0f}°", "Clockwise from true north"),
            ]
        )
        _packing_cross_check(sc, layout)
        st.plotly_chart(
            charts.layout_plan(
                layout.polygon_m, layout.holes_m,
                geometry.module_outlines(layout, sc.array.module, sc.array.orientation),
            ),
            use_container_width=True,
        )

    assumptions_panel(sc, "array")


def _module_picker(sc: Scenario) -> None:
    source = st.radio("Module source", ["CEC database", "Enter manually"], horizontal=True)
    if source == "CEC database":
        query = st.text_input("Search the CEC module database", "", placeholder="e.g. LONGi 550")
        try:
            names = modules.search(query, limit=40) if query else modules.search("", limit=40)
        except Exception as exc:  # pragma: no cover - depends on the pvlib data files
            st.error(f"Could not read the CEC database: {exc}")
            return
        if not names:
            st.warning("No modules matched that search.")
            return
        chosen = st.selectbox("Module", names)
        module = modules.get(chosen)
        st.caption(
            f"{module.stc_watts:.0f} W · {module.width_m:.3f} × {module.height_m:.3f} m · "
            f"{module.technology}"
        )
        st.caption(
            "Dimensions are derived from the database's cell area — the CEC file carries no "
            "frame size. Override them below if you have the datasheet."
        )
    else:
        name = st.text_input("Module name", "Custom module")
        width = st.number_input("Width (m)", 0.3, 2.5, 1.134, 0.001, format="%.3f")
        height = st.number_input("Height (m)", 0.5, 3.0, 2.278, 0.001, format="%.3f")
        watts = st.number_input("STC power (W)", 50.0, 900.0, 550.0, 5.0)
        module = modules.custom(name, width, height, watts)

    for warning in modules.sanity_check(module):
        st.warning(warning)
    if module != sc.array.module:
        sc.array = replace(sc.array, module=module)
        sc.invalidate_from("array")


def _packing_cross_check(sc: Scenario, layout) -> None:
    """Compare packing density against the cited layout benchmark, if there is one."""
    try:
        row = table().find("Roof area per kWp on a flat roof", market=sc.site.market.value)
        low, high = row.value(benchmarks.Band.LOW), row.value(benchmarks.Band.HIGH)
    except (benchmarks.BenchmarkNotFound, benchmarks.GapError):
        return
    density = layout.area_per_kwp_m2
    note = f"{density:.1f} m²/kWp against a benchmark of {low:g}–{high:g} ({row.citation()})"
    if low <= density <= high:
        st.caption(f"Cross-check: {note}.")
    else:
        st.warning(f"Cross-check: {note}. Check tilt, setback and module dimensions.")


# ---------------------------------------------------------------------------
# 3. Yield
# ---------------------------------------------------------------------------


def screen_yield(sc: Scenario) -> None:
    st.header("Yield")
    if sc.layout is None or sc.layout.module_count == 0:
        st.warning("Pack an array first.")
        return

    centre = sc.site.centre
    loss = st.slider(
        "System loss (%)", 0.0, 30.0,
        float(sc.yield_result.system_loss_pct if sc.yield_result else resource.DEFAULT_LOSS_PCT),
        0.5,
        help="PVGIS applies this to cabling, inverter, mismatch and soiling. Its own default is 14%.",
    )
    mounting = "building" if sc.array.array_type is ArrayType.ROOFTOP else "free"

    if st.button("Fetch PVGIS hourly data", type="primary") or sc.yield_result is not None:
        if sc.yield_result is None:
            with st.spinner("Calling PVGIS — the first call for a site is slow, then it is cached"):
                try:
                    sc.yield_result = resource.hourly_yield(
                        lat=centre[1], lon=centre[0], tilt_deg=sc.layout.tilt_deg,
                        azimuth_deg=sc.layout.azimuth_deg, kwp=sc.layout.kwp,
                        loss_pct=loss, mounting=mounting,
                    )
                except resource.PVGISError as exc:
                    st.error(str(exc))
                    return

    result = sc.yield_result
    if result is None:
        st.info("No yield data yet.")
        assumptions_panel(sc, "yield")
        return

    metric_row(
        [
            ("Annual generation", f"{result.annual_kwh:,.0f} kWh", result.source),
            ("Specific yield", f"{result.specific_yield_kwh_per_kwp:,.0f} kWh/kWp", None),
            ("Capacity", f"{result.kwp:,.1f} kWp", None),
            ("System loss", f"{result.system_loss_pct:.0f} %", "As applied by PVGIS"),
        ]
    )
    _yield_cross_check(sc, result)

    st.plotly_chart(charts.monthly_generation(result.monthly_kwh), use_container_width=True)
    if result.hourly_kwh:
        st.plotly_chart(
            charts.hourly_heatmap(resource.hourly_to_heatmap(result.hourly_kwh)),
            use_container_width=True,
        )
    st.caption(f"Source: {result.source}. Data period {result.year_range}.")
    assumptions_panel(sc, "yield")


def _yield_cross_check(sc: Scenario, result) -> None:
    """Sanity check specific yield against the market's cited rule of thumb."""
    lookups = {
        Market.UK: None,
        Market.INDIA: ("Typical daily specific yield", 365.0),
        Market.UAE: ("Peak sun hours Dubai", 1.0),
    }
    lookup = lookups.get(sc.site.market)
    if lookup is None:
        return
    parameter, scale = lookup
    try:
        row = table().find(parameter, market=sc.site.market.value)
        low = row.value(benchmarks.Band.LOW) * scale
        high = row.value(benchmarks.Band.HIGH) * scale
    except (benchmarks.BenchmarkNotFound, benchmarks.GapError):
        return
    message = resource.sanity_check_specific_yield(result, low, high)
    if message:
        st.warning(f"{message}. Benchmark: {row.citation()}.")
    else:
        st.caption(
            f"Cross-check: within the {low:,.0f}–{high:,.0f} kWh/kWp band from {row.citation()}."
        )


# ---------------------------------------------------------------------------
# 4. Storage
# ---------------------------------------------------------------------------


def screen_storage(sc: Scenario) -> None:
    st.header("Storage")
    if sc.yield_result is None or not sc.yield_result.hourly_kwh:
        st.warning("Fetch hourly yield data first — the battery model needs all 8760 hours.")
        return

    left, right = st.columns([1, 2])
    with left:
        annual_load = st.number_input(
            "Annual site load (kWh)", 0.0, 50_000_000.0,
            float(sc.load.annual_kwh or sc.yield_result.annual_kwh), 1000.0,
        )
        shape = st.selectbox("Load shape", dispatch.shapes(),
                             index=dispatch.shapes().index("office"))
        weekend = st.slider("Weekend factor", 0.0, 1.5, 1.0, 0.05)
        st.caption(
            "These shapes are synthetic, not metered. Replace them with the client's "
            "half-hourly data before any figure leaves the building."
        )
        max_size = st.number_input("Largest battery to test (kWh)", 1.0, 20_000.0,
                                   max(10.0, round(sc.layout.kwp * 2)), 5.0)
        rte = st.slider("Round-trip efficiency (%)", 70.0, 100.0, 90.0, 1.0)

    sc.load = dispatch.synthetic_load_profile(annual_load, shape=shape, weekend_factor=weekend)
    sizes = [max_size * step / 8.0 for step in range(9)]
    points = dispatch.sweep(
        sc.yield_result.hourly_kwh, sc.load.hourly_kwh, sizes,
        round_trip_efficiency=rte / 100.0,
    )

    with right:
        st.plotly_chart(
            charts.self_consumption_curve(
                [p.usable_kwh for p in points], [p.self_consumption_fraction for p in points]
            ),
            use_container_width=True,
        )
        st.plotly_chart(
            charts.load_overlay(
                _average_day(sc.yield_result.hourly_kwh), _average_day(sc.load.hourly_kwh)
            ),
            use_container_width=True,
        )

    chosen_size = st.select_slider(
        "Battery size to carry into the financial model (usable kWh)",
        options=[round(p.usable_kwh, 1) for p in points],
        value=round(points[0].usable_kwh, 1),
    )
    chosen = min(points, key=lambda p: abs(p.usable_kwh - chosen_size))
    sc.battery = BatterySpec(
        usable_kwh=chosen.usable_kwh, power_kw=chosen.usable_kwh * 0.5,
        round_trip_efficiency=rte / 100.0,
    )
    sc.dispatch = chosen.result

    metric_row(
        [
            ("Self-consumption", f"{chosen.self_consumption_fraction:.1%}", None),
            ("Imported", f"{chosen.result.imported_kwh:,.0f} kWh", "Still bought from the grid"),
            ("Exported", f"{chosen.result.exported_kwh:,.0f} kWh", None),
            ("Full cycles", f"{chosen.result.cycles:,.0f} /yr", "Charge throughput over usable capacity"),
        ]
    )
    st.caption(f"Load profile: {sc.load.label}")
    assumptions_panel(sc, "storage")


def _average_day(hourly: list[float]) -> list[float]:
    """Mean value for each hour of the day across the year. Presentation only."""
    return [sum(hourly[hour::24]) / max(1, len(hourly[hour::24])) for hour in range(24)]


# ---------------------------------------------------------------------------
# 5. Finance
# ---------------------------------------------------------------------------


def screen_finance(sc: Scenario) -> None:
    st.header("Finance")
    if sc.yield_result is None:
        st.warning("Fetch yield data first.")
        return

    market = sc.site.market.value
    market_table = table().for_market(market)
    currency = market_table.currency_for(market)
    gap_warning(sc)

    left, right = st.columns(2)
    with left:
        st.subheader("From the benchmark data")
        capex = _benchmark_input(
            "Installed cost", market, "capex", currency + " per kWp", sc.layout.kwp
        )
        opex = st.number_input(f"Annual opex ({currency}/yr)", 0.0, 1e9,
                               float(sc.finance_inputs.opex_per_year), 100.0)
        incentives = st.number_input(
            f"Year-one incentives ({currency})", 0.0, 1e9,
            float(sc.finance_inputs.incentives_year_one), 100.0,
            help="PM Surya Ghar CFA, Annual Investment Allowance and similar. See the "
                 "assumptions table for the cited slab rates.",
        )
        grid_factor = _grid_factor_input(market)

    with right:
        st.subheader("Your inputs — no defensible default exists")
        st.caption("These rows are tiered `gap` in the source data. They are never pre-filled.")
        discount = st.number_input("Discount rate (%)", 0.0, 50.0, 0.0, 0.5,
                                   help="Prompts only: UK 6–8%, India 10–12%, UAE 7–9%.")
        escalation = st.number_input("Tariff escalation (% per year)", -10.0, 30.0, 0.0, 0.1)
        import_tariff = st.number_input(f"Import tariff ({currency}/kWh)", 0.0, 100.0, 0.0, 0.01,
                                        format="%.4f")
        export_tariff = st.number_input(f"Export tariff ({currency}/kWh)", 0.0, 100.0, 0.0, 0.01,
                                        format="%.4f")
        life = st.number_input("Project life (years)", 1, 40,
                               int(market_table.get("Project life")))
        degradation = st.number_input(
            "Module degradation (% per year)", 0.0, 3.0,
            float(market_table.get("Module degradation")), 0.05,
        )

    sc.finance_inputs = FinanceInputs(
        discount_rate=discount / 100.0 if discount > 0.0 else None,
        tariff_escalation=escalation,
        project_life_years=int(life),
        degradation_pct_per_year=degradation,
        import_tariff_per_kwh=import_tariff if import_tariff > 0.0 else None,
        export_tariff_per_kwh=export_tariff,
        grid_emission_factor_t_per_mwh=grid_factor,
        capex_per_kwp=capex,
        opex_per_year=opex,
        incentives_year_one=incentives,
        currency=currency,
    )

    missing = sc.finance_inputs.missing()
    if missing:
        st.warning(
            "Waiting on: " + ", ".join(m.replace("_", " ") for m in missing)
            + ". Nothing is computed until every one is supplied."
        )
        assumptions_panel(sc, "finance")
        return

    balance = _energy_balance(sc)
    try:
        result = finance.evaluate(balance, sc.finance_inputs)
    except finance.FinanceError as exc:
        st.error(str(exc))
        return
    sc.finance = result

    metric_row(
        [
            ("NPV", f"{result.npv:,.0f} {currency}", f"At {discount:.1f}% over {life} years"),
            ("IRR", "—" if result.irr is None else f"{result.irr:.1%}", None),
            ("Payback",
             "—" if result.simple_payback_years is None else f"{result.simple_payback_years:.1f} yr",
             "Simple, undiscounted"),
            ("LCOE", f"{result.lcoe_per_kwh:,.3f} {currency}/kWh", None),
        ]
    )
    st.plotly_chart(charts.cashflow_waterfall(result.cashflows, currency), use_container_width=True)
    st.plotly_chart(
        charts.cumulative_discounted(
            result.discounted_cashflows, currency, result.discounted_payback_years
        ),
        use_container_width=True,
    )

    _macc_section(sc, balance, grid_factor, currency)
    assumptions_panel(sc, "finance")


def _energy_balance(sc: Scenario) -> finance.EnergyBalance:
    if sc.dispatch is not None:
        return finance.EnergyBalance(
            kwp=sc.layout.kwp,
            generation_kwh=sc.yield_result.annual_kwh,
            self_consumed_kwh=sc.dispatch.self_consumed_kwh + sc.dispatch.discharged_kwh,
            exported_kwh=sc.dispatch.exported_kwh,
        )
    # No dispatch run: everything is treated as exported, which is the
    # pessimistic read and is flagged on screen.
    st.caption(
        "No dispatch run — the whole output is valued at the export tariff. "
        "Run the Storage screen for a self-consumption split."
    )
    return finance.EnergyBalance(
        kwp=sc.layout.kwp, generation_kwh=sc.yield_result.annual_kwh,
        self_consumed_kwh=0.0, exported_kwh=sc.yield_result.annual_kwh,
    )


def _benchmark_input(label: str, market: str, category: str, unit: str, kwp: float) -> float:
    """A capex figure chosen from the cited rows, with its tier shown."""
    rows = [r for r in table().for_market(market, include_all=False).category(category)
            if not r.is_gap and "cost" in r.parameter.lower()]
    if not rows:
        return st.number_input(f"{label} ({unit})", 0.0, 1e7, 0.0, 10.0)
    chosen = st.selectbox(
        f"{label} benchmark", rows,
        format_func=lambda r: f"{r.parameter} [{r.source_tier.value}]",
        help="Every row is cited. Tier is shown in brackets.",
    )
    band = st.radio("Band", list(benchmarks.Band), horizontal=True, index=1,
                    format_func=lambda b: b.value.title())
    value = chosen.value(band)
    per_kwp = value * 1000.0 if chosen.unit == "per_Wp" else value
    st.caption(
        f"{value:g} {chosen.unit} {chosen.currency} → {per_kwp:,.0f} {unit}. "
        f"Source: {chosen.citation()}. {chosen.remarks}"
    )
    return st.number_input(f"{label} used ({unit})", 0.0, 1e7, float(per_kwp), 10.0)


def _grid_factor_input(market: str) -> float | None:
    """Grid emission factor, in tCO2e/MWh, from the cited rows where one exists."""
    rows = [r for r in table().for_market(market, include_all=False).category("carbon")]
    usable = [r for r in rows if not r.is_gap]
    if not usable:
        st.warning(
            "No present-day grid emission factor is available for this market — every "
            "carbon row is tiered `gap`. Enter one or the abatement curve stays empty."
        )
        entered = st.number_input("Grid emission factor (tCO2e/MWh)", 0.0, 2.0, 0.0, 0.001,
                                  format="%.3f")
        return entered if entered > 0.0 else None
    chosen = st.selectbox(
        "Grid emission factor", usable,
        format_func=lambda r: f"{r.parameter} [{r.source_tier.value}]",
    )
    value = chosen.value()
    # kgCO2e/kWh and tCO2/MWh are the same number; the CSV uses both labels.
    st.caption(f"{value:g} {chosen.unit}. Source: {chosen.citation()}. {chosen.remarks}")
    return value


def _macc_section(sc: Scenario, balance: finance.EnergyBalance, grid_factor: float | None,
                  currency: str) -> None:
    st.subheader("Marginal abatement cost curve")
    if grid_factor is None:
        st.info("No grid emission factor supplied, so there is no abatement to plot.")
        return
    life = int(sc.finance_inputs.project_life_years)
    generation = finance.degraded_generation(
        balance.generation_kwh, sc.finance_inputs.degradation_pct_per_year, life
    )
    tranches = 3
    steps = [
        finance.macc_step(
            f"PV tranche {index + 1}",
            delta_capex=sc.finance.total_capex / tranches,
            delta_generation_kwh=[value / tranches for value in generation],
            grid_factor_t_per_mwh=grid_factor,
            currency=currency,
        )
        for index in range(tranches)
    ]
    sc.macc = steps
    st.plotly_chart(charts.macc(finance.macc_curve(steps), currency), use_container_width=True)
    st.caption(
        "Bars are marginal and additive: each is the cost and abatement of adding that step "
        "on top of everything to its left. Battery and cleaning tranches are not modelled yet."
    )


# ---------------------------------------------------------------------------
# 6. Report
# ---------------------------------------------------------------------------


def screen_report(sc: Scenario) -> None:
    st.header("Report")
    if sc.layout is None:
        st.warning("There is nothing to report yet.")
        return

    st.subheader(sc.site.name)
    metric_row(
        [
            ("Capacity", f"{sc.layout.kwp:,.1f} kWp", f"{sc.layout.module_count:,} modules"),
            ("Annual generation",
             "—" if sc.yield_result is None else f"{sc.yield_result.annual_kwh:,.0f} kWh", None),
            ("NPV", "—" if sc.finance is None else f"{sc.finance.npv:,.0f} {sc.finance.currency}",
             None),
            ("Payback",
             "—" if sc.finance is None or sc.finance.simple_payback_years is None
             else f"{sc.finance.simple_payback_years:.1f} yr", None),
        ]
    )

    html = _report_html(sc)
    st.download_button(
        "Download the one-page report", html,
        file_name=f"{sc.site.name.replace(' ', '_').lower()}_arka_report.html",
        mime="text/html", type="primary",
    )
    st.download_button(
        "Download the scenario as JSON", sc.to_json(include_hourly=False),
        file_name=f"{sc.site.name.replace(' ', '_').lower()}_scenario.json",
        mime="application/json",
    )
    _narrative_section(sc)
    render_html(html, height=700, scrolling=True)


def _report_html(sc: Scenario) -> str:
    """One-page HTML export. Every figure is quoted from the scenario."""
    rows = table().assumptions(sc.site.market.value)
    assumption_rows = "".join(
        f"<tr><td>{r['parameter']}</td><td>{r['value']}</td><td>{r['unit']}</td>"
        f"<td><span class='tier tier-{r['tier']}'>{r['tier']}</span></td>"
        f"<td><a href='{r['url']}'>{r['source']}</a></td></tr>"
        for r in rows
    )
    figures = [("Capacity", f"{sc.layout.kwp:,.1f} kWp"),
               ("Modules", f"{sc.layout.module_count:,}"),
               ("Usable area", f"{sc.layout.usable_area_m2:,.0f} m²"),
               ("Azimuth", f"{sc.layout.azimuth_deg:.0f}°"),
               ("Tilt", f"{sc.layout.tilt_deg:.0f}°")]
    if sc.yield_result:
        figures += [
            ("Annual generation", f"{sc.yield_result.annual_kwh:,.0f} kWh"),
            ("Specific yield", f"{sc.yield_result.specific_yield_kwh_per_kwp:,.0f} kWh/kWp"),
        ]
    if sc.dispatch:
        figures += [("Self-consumption", f"{sc.dispatch.self_consumption_fraction:.1%}"),
                    ("Battery", f"{sc.battery.usable_kwh:,.1f} kWh usable")]
    if sc.finance:
        figures += [
            ("NPV", f"{sc.finance.npv:,.0f} {sc.finance.currency}"),
            ("IRR", "—" if sc.finance.irr is None else f"{sc.finance.irr:.1%}"),
            ("LCOE", f"{sc.finance.lcoe_per_kwh:,.3f} {sc.finance.currency}/kWh"),
        ]
    figure_rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in figures)

    plan = ""
    if sc.array.module and sc.layout.placements:
        plan = charts.layout_plan(
            sc.layout.polygon_m, sc.layout.holes_m,
            geometry.module_outlines(sc.layout, sc.array.module, sc.array.orientation),
        ).to_html(include_plotlyjs="cdn", full_html=False)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{sc.site.name} — Arka</title>
<style>
 body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 2rem auto;
        max-width: 60rem; color: #16181d; line-height: 1.5; }}
 h1 {{ margin-bottom: 0.2rem; }} .sub {{ color: #666; margin-top: 0; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
 th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #e3e5e8; }}
 th {{ white-space: nowrap; }}
 .tier {{ font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 0.6rem; }}
 .tier-authoritative {{ background: #d7f2e0; }} .tier-market-survey {{ background: #e2ecfa; }}
 .tier-installer-marketing {{ background: #fdf0d5; }} .tier-convention {{ background: #eceef1; }}
 .tier-gap {{ background: #fadbd8; }}
 footer {{ color: #666; font-size: 0.85rem; margin-top: 2rem; }}
</style></head><body>
<h1>{sc.site.name}</h1>
<p class="sub">{sc.site.market.value} · {sc.array.array_type.value.replace('_', ' ').title()} ·
{sc.array.mounting.value.title()}</p>
<h2>Results</h2><table>{figure_rows}</table>
<h2>Layout</h2>{plan or "<p>No layout available.</p>"}
<h2>Assumptions</h2>
<p>Source tier sits next to every value. Rows marked <span class="tier tier-gap">gap</span>
have no defensible open source and were supplied as inputs.</p>
<table><tr><th>Parameter</th><th>Value</th><th>Unit</th><th>Tier</th><th>Source</th></tr>
{assumption_rows}</table>
<footer>Irradiance: {sc.yield_result.source if sc.yield_result else 'not fetched'}.
Generated by Arka. Every figure above is computed by pvlib, the dispatch model or the cited
benchmark data — none is produced by a language model.</footer>
</body></html>"""


def _narrative_section(sc: Scenario) -> None:
    st.subheader("Narrative")
    agent = agent_client.ArkaAgent()
    if not agent.available:
        st.caption(
            f"Set {agent_client.API_KEY_ENV} to have the model draft the narrative. "
            "Every figure would still come from the computed results — the model is "
            "screened for numbers it was not given."
        )
        return
    if st.button("Draft the narrative"):
        figures = sc.to_dict(include_hourly=False)
        try:
            narrative = agent.write_narrative(figures)
        except agent_client.NumberGuardError as exc:
            st.error(f"Draft rejected. {exc}")
            return
        except agent_client.AgentError as exc:
            st.error(str(exc))
            return
        st.markdown(f"**{narrative.headline}**")
        st.write(narrative.body)
        for caveat in narrative.caveats:
            st.caption(f"· {caveat}")


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


SCREEN_FUNCTIONS = {
    "Site": screen_site,
    "Array": screen_array,
    "Yield": screen_yield,
    "Storage": screen_storage,
    "Finance": screen_finance,
    "Report": screen_report,
}


def main() -> None:
    st.set_page_config(page_title="Arka — PV siting and sizing", layout="wide")
    sc = scenario()

    with st.sidebar:
        st.title("Arka")
        st.caption("PV siting and sizing from open data.")
        screen = st.radio("Screen", SCREENS, label_visibility="collapsed")
        st.divider()
        st.caption(
            "No figure on any screen is produced by a language model. Every one comes "
            "from pvlib, the dispatch model, or the cited benchmark data."
        )
        uploaded = st.file_uploader("Load a scenario", type="json")
        if uploaded is not None and st.button("Load"):
            st.session_state.scenario = Scenario.from_json(uploaded.read().decode())
            st.rerun()

    SCREEN_FUNCTIONS[screen](sc)


if __name__ == "__main__":
    main()

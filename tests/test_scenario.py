"""State: serialisation, invalidation and the derived properties."""

from __future__ import annotations

import pytest

from arka.scenario import (
    ArraySpec,
    ArrayType,
    BatterySpec,
    DispatchResult,
    FinanceInputs,
    Layout,
    Market,
    ModulePlacement,
    ModuleSpec,
    Mounting,
    Orientation,
    Scenario,
    Site,
    YieldResult,
    isclose,
)


def a_scenario() -> Scenario:
    scenario = Scenario(
        site=Site(
            name="Warehouse roof",
            market=Market.INDIA,
            boundary=[(72.87, 19.07), (72.871, 19.07), (72.871, 19.071), (72.87, 19.071)],
            exclusions=[[(72.8705, 19.0705), (72.8706, 19.0705), (72.8706, 19.0706)]],
        ),
        array=ArraySpec(
            array_type=ArrayType.ROOFTOP,
            mounting=Mounting.BALLASTED,
            module=ModuleSpec("Test 550", 1.134, 2.278, 550.0),
            orientation=Orientation.LANDSCAPE,
            tilt_deg=12.0,
            azimuth_deg=175.0,
        ),
        battery=BatterySpec(usable_kwh=20.0, power_kw=10.0),
        finance_inputs=FinanceInputs(discount_rate=0.11, currency="INR"),
        notes="drawn from the Feb site visit",
    )
    scenario.layout = Layout(
        module_count=120, kwp=66.0, rows=8, gross_area_m2=900.0, usable_area_m2=820.0,
        module_area_m2=310.0, row_pitch_m=3.4, azimuth_deg=175.0, tilt_deg=12.0,
        placements=[ModulePlacement(1.0, 2.0, 0, 0), ModulePlacement(2.2, 2.0, 0, 1)],
        polygon_m=[(0.0, 0.0), (30.0, 0.0), (30.0, 30.0)],
    )
    scenario.yield_result = YieldResult(
        annual_kwh=95_000.0, monthly_kwh=[8000.0] * 12, hourly_kwh=[1.0] * 8760, kwp=66.0,
        year_range=(2005, 2023),
    )
    scenario.dispatch = DispatchResult(
        self_consumed_kwh=60_000.0, exported_kwh=30_000.0, imported_kwh=15_000.0,
        charged_kwh=5_000.0, discharged_kwh=4_500.0,
    )
    return scenario


# -- round trips ------------------------------------------------------------


def test_scenario_round_trips_through_json():
    original = a_scenario()
    restored = Scenario.from_json(original.to_json())

    assert restored.site.name == original.site.name
    assert restored.site.market is Market.INDIA
    assert restored.array.array_type is ArrayType.ROOFTOP
    assert restored.array.orientation is Orientation.LANDSCAPE
    assert restored.array.module.stc_watts == pytest.approx(550.0)
    assert restored.layout.module_count == 120
    assert restored.layout.kwp == pytest.approx(66.0)
    assert restored.yield_result.annual_kwh == pytest.approx(95_000.0)
    assert restored.dispatch.self_consumed_kwh == pytest.approx(60_000.0)
    assert restored.finance_inputs.currency == "INR"
    assert restored.notes == original.notes


def test_placements_survive_the_round_trip():
    restored = Scenario.from_json(a_scenario().to_json())
    assert len(restored.layout.placements) == 2
    assert restored.layout.placements[1].col == 1


def test_hourly_series_can_be_dropped_for_a_compact_save():
    compact = a_scenario().to_dict(include_hourly=False)
    assert compact["yield_result"]["hourly_kwh"] == []
    assert compact["yield_result"]["annual_kwh"] == pytest.approx(95_000.0)


def test_an_empty_scenario_round_trips():
    restored = Scenario.from_json(Scenario().to_json())
    assert restored.layout is None
    assert restored.yield_result is None
    assert restored.site.boundary == []


def test_enums_serialise_as_plain_strings():
    data = a_scenario().to_dict()
    assert data["site"]["market"] == "India"
    assert data["array"]["array_type"] == "ROOFTOP"


# -- derived properties -----------------------------------------------------


def test_site_centre_is_the_vertex_mean():
    site = Site(boundary=[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)])
    assert site.centre == pytest.approx((1.0, 1.0))


def test_site_without_a_boundary_has_no_centre():
    assert Site().centre is None


def test_specific_yield_divides_by_capacity():
    result = YieldResult(annual_kwh=100_000.0, monthly_kwh=[0.0] * 12, kwp=80.0)
    assert result.specific_yield_kwh_per_kwp == pytest.approx(1250.0)


def test_specific_yield_of_a_zero_capacity_array_is_zero():
    assert YieldResult(0.0, [0.0] * 12, kwp=0.0).specific_yield_kwh_per_kwp == pytest.approx(0.0)


def test_module_footprint_swaps_with_orientation():
    module = ModuleSpec("m", width_m=1.1, height_m=2.3, stc_watts=550.0)
    assert module.footprint(Orientation.PORTRAIT) == (1.1, 2.3)
    assert module.footprint(Orientation.LANDSCAPE) == (2.3, 1.1)


def test_ground_coverage_ratio_and_area_per_kwp():
    layout = a_scenario().layout
    assert layout.ground_coverage_ratio == pytest.approx(310.0 / 820.0)
    assert layout.area_per_kwp_m2 == pytest.approx(820.0 / 66.0)


def test_scenario_kwp_reads_through_to_the_layout():
    assert a_scenario().kwp == pytest.approx(66.0)
    assert Scenario().kwp == pytest.approx(0.0)


# -- invalidation -----------------------------------------------------------


def test_changing_the_site_drops_everything_downstream():
    scenario = a_scenario()
    scenario.invalidate_from("site")
    assert scenario.layout is None
    assert scenario.yield_result is None
    assert scenario.dispatch is None


def test_changing_storage_keeps_the_layout_and_yield():
    scenario = a_scenario()
    scenario.invalidate_from("storage")
    assert scenario.layout is not None
    assert scenario.yield_result is not None
    assert scenario.dispatch is None


def test_an_unknown_stage_is_rejected():
    with pytest.raises(ValueError):
        Scenario().invalidate_from("nonsense")


# -- float discipline -------------------------------------------------------


def test_isclose_is_available_instead_of_equality():
    assert isclose(0.1 + 0.2, 0.3)
    assert not isclose(0.1, 0.2)


# -- the gap rule reaches the state layer -----------------------------------


def test_finance_inputs_default_the_gap_rows_to_none():
    inputs = FinanceInputs()
    assert inputs.discount_rate is None
    assert inputs.tariff_escalation is None
    assert "discount_rate" in inputs.missing()


def test_a_pasted_view_centre_round_trips():
    scenario = Scenario(site=Site(view_centre=(55.2708, 25.2048)))
    restored = Scenario.from_json(scenario.to_json())
    assert restored.site.view_centre == pytest.approx((55.2708, 25.2048))
    assert restored.site.boundary == []


def test_view_centre_defaults_to_none():
    assert Site().view_centre is None
    assert Scenario.from_json(Scenario().to_json()).site.view_centre is None

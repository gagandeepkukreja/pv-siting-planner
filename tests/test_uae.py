"""The UAE market end to end.

The UAE is the market that exercises the awkward paths: capex quoted per Wp
rather than per kWp, no cited battery cost, no capital subsidy, a present-day
grid emission factor that is deliberately absent, a yield benchmark given as a
single figure of irradiation rather than a band of output, and a net-metering
scheme that caps the value of oversizing.
"""

from __future__ import annotations

import pytest

from arka import benchmarks, dispatch, finance, incentives, resource
from arka.scenario import BatterySpec, FinanceInputs, YieldResult


@pytest.fixture(scope="module")
def table():
    return benchmarks.load()


# -- units and currency ------------------------------------------------------


def test_uae_prices_are_in_dirhams(table):
    assert table.currency_for("UAE") == "AED"


def test_uae_capex_is_quoted_per_watt_not_per_kilowatt(table):
    """Every other market quotes per kWp. Getting this wrong is a 1000x error."""
    row = table.find("PV installed cost commercial >50 kWp", "UAE")
    assert row.unit == "per_Wp"
    assert row.value() * 1000.0 == pytest.approx(1_600.0)


def test_commercial_beats_residential_per_watt(table):
    residential = table.get("PV installed cost residential 3-10 kWp", "UAE")
    commercial = table.get("PV installed cost commercial >50 kWp", "UAE")
    assert commercial < residential


# -- what the UAE does not have ---------------------------------------------


def test_no_capital_incentive_is_a_zero_not_a_gap(table):
    """The UAE's incentive is net metering, which lands in the energy balance."""
    result = incentives.for_market("UAE", 8.5, 40_000.0, table)
    assert result.amount == pytest.approx(0.0)
    assert result.scheme == "none"


def test_no_battery_cost_is_cited(table):
    rows = [r for r in table.for_market("UAE", include_all=False).category("capex")
            if "battery" in r.parameter.lower() and not r.is_gap]
    assert rows == [], "a cited UAE battery row would change how the UI prompts"


def test_the_present_day_grid_factor_is_deliberately_absent(table):
    """Historic baseline and forward target exist; neither is a current factor."""
    assert table.find("Grid emission factor present day", "UAE").is_gap
    assert not table.find("Grid emission factor 2019 baseline", "UAE").is_gap
    assert not table.find("Grid emission factor 2030 target", "UAE").is_gap


def test_the_2030_target_is_not_mistaken_for_today(table):
    baseline = table.get("Grid emission factor 2019 baseline", "UAE")
    target = table.get("Grid emission factor 2030 target", "UAE")
    assert target < baseline
    assert "Do NOT use as a present-day factor" in table.find(
        "Grid emission factor 2030 target", "UAE"
    ).remarks


# -- the yield cross-check ---------------------------------------------------


def test_peak_sun_hours_are_irradiation_not_output():
    """2200 peak sun hours does not mean 2200 kWh/kWp delivered."""
    assert resource.peak_sun_hours_to_specific_yield(2200.0, 14.0) == pytest.approx(1892.0)


def test_a_single_figure_benchmark_is_widened(table):
    """The Dubai row quotes one number, so a zero-width band would flag
    every correct result."""
    row = table.find("Peak sun hours Dubai", "UAE")
    assert row.low == row.high
    low, high = resource.widen_point_benchmark(row.low, row.high)
    assert low < row.central < high


def test_a_realistic_dubai_yield_passes_the_cross_check():
    expected = resource.peak_sun_hours_to_specific_yield(2200.0, 14.0)
    for specific in (1700.0, 1892.0, 2000.0):
        result = YieldResult(annual_kwh=specific * 10.0, monthly_kwh=[0.0] * 12, kwp=10.0)
        assert resource.sanity_check_specific_yield(result, expected, expected) is None


def test_an_implausible_dubai_yield_is_still_flagged():
    expected = resource.peak_sun_hours_to_specific_yield(2200.0, 14.0)
    poor = YieldResult(annual_kwh=1_200.0 * 10.0, monthly_kwh=[0.0] * 12, kwp=10.0)
    assert "below" in resource.sanity_check_specific_yield(poor, expected, expected)


# -- net metering ------------------------------------------------------------


def balance(generation=16_000.0, self_consumed=10_000.0, exported=6_000.0, imported=8_000.0):
    return finance.EnergyBalance(
        kwp=8.5, generation_kwh=generation, self_consumed_kwh=self_consumed,
        exported_kwh=exported, imported_kwh=imported,
    )


def test_without_net_metering_all_export_is_paid_at_the_export_rate():
    credited, paid = balance().export_split(net_metering=False)
    assert credited == pytest.approx(0.0)
    assert paid == pytest.approx(6_000.0)


def test_export_is_credited_only_against_a_real_import():
    credited, paid = balance(exported=6_000.0, imported=8_000.0).export_split(True)
    assert credited == pytest.approx(6_000.0)
    assert paid == pytest.approx(0.0)


def test_export_beyond_consumption_earns_only_the_surplus_rate():
    """The ceiling that caps oversizing."""
    credited, paid = balance(exported=12_000.0, imported=5_000.0).export_split(True)
    assert credited == pytest.approx(5_000.0)
    assert paid == pytest.approx(7_000.0)


def test_a_site_that_never_imports_credits_nothing():
    credited, paid = balance(exported=6_000.0, imported=0.0).export_split(True)
    assert credited == pytest.approx(0.0)
    assert paid == pytest.approx(6_000.0)


def uae_inputs(**overrides):
    base = dict(
        discount_rate=0.08, tariff_escalation=2.0, project_life_years=25,
        degradation_pct_per_year=0.5, import_tariff_per_kwh=0.38,
        export_tariff_per_kwh=0.0, capex_per_kwp=2_300.0, opex_per_year=3_650.0,
        net_metering=True, currency="AED",
    )
    base.update(overrides)
    return FinanceInputs(**base)


def test_net_metering_beats_unpaid_export():
    """With DEWA paying nothing for export, crediting it is worth a great deal."""
    credited = finance.evaluate(balance(), uae_inputs())
    unpaid = finance.evaluate(balance(), uae_inputs(net_metering=False))
    assert credited.npv > unpaid.npv


def test_oversizing_stops_paying_once_consumption_is_cancelled():
    """The effect the benchmark data calls the driver of the battery case."""
    modest = finance.evaluate(
        balance(generation=16_000.0, exported=6_000.0, imported=8_000.0), uae_inputs()
    )
    double = finance.evaluate(
        balance(generation=32_000.0, exported=22_000.0, imported=8_000.0), uae_inputs()
    )
    quadruple = finance.evaluate(
        balance(generation=64_000.0, exported=54_000.0, imported=8_000.0), uae_inputs()
    )
    assert double.npv > modest.npv          # up to the ceiling, more still helps
    assert quadruple.npv == pytest.approx(double.npv, rel=1e-9)   # past it, nothing


def test_the_uae_lump_sums_reach_the_capex(table):
    lumps = {
        r.parameter: r.value()
        for r in table.for_market("UAE", include_all=False).category("capex")
        if r.unit == "lump_sum" and not r.is_gap
    }
    assert "Grid connection fee and bidirectional meter" in lumps
    plain = finance.total_capex(balance(), uae_inputs())
    with_lumps = finance.total_capex(balance(), uae_inputs(capex_lump_sums=lumps))
    assert with_lumps - plain == pytest.approx(sum(lumps.values()))


def test_the_inverter_replacement_reaches_year_twelve(table):
    cost = table.get("Inverter replacement year 8-12", "UAE")
    flows = finance.build_cashflows(balance(), uae_inputs(year_costs={12: cost}))
    plain = finance.build_cashflows(balance(), uae_inputs())
    assert plain[12] - flows[12] == pytest.approx(cost)


def test_uae_running_costs_are_material(table):
    """Cleaning and insurance are easy to leave out of a quote and change the answer."""
    opex = sum(r.value() for r in table.for_market("UAE", include_all=False).category("opex")
               if r.unit.startswith("per_year"))
    assert opex > 0.0
    lean = finance.evaluate(balance(), uae_inputs(opex_per_year=0.0))
    real = finance.evaluate(balance(), uae_inputs(opex_per_year=opex))
    assert real.npv < lean.npv

"""Appraisal arithmetic and the abatement curve."""

from __future__ import annotations

import pytest

from arka import finance
from arka.scenario import FinanceInputs, MaccStep


def balance(kwp: float = 100.0, generation: float = 100_000.0,
            self_consumed: float = 70_000.0, exported: float = 30_000.0) -> finance.EnergyBalance:
    return finance.EnergyBalance(
        kwp=kwp,
        generation_kwh=generation,
        self_consumed_kwh=self_consumed,
        exported_kwh=exported,
    )


def inputs(**overrides) -> FinanceInputs:
    base = dict(
        discount_rate=0.07,
        tariff_escalation=0.0,
        project_life_years=25,
        degradation_pct_per_year=0.5,
        import_tariff_per_kwh=0.30,
        export_tariff_per_kwh=0.05,
        capex_per_kwp=900.0,
        opex_per_year=1_000.0,
        currency="GBP",
    )
    base.update(overrides)
    return FinanceInputs(**base)


# -- primitives -------------------------------------------------------------


def test_npv_of_a_flat_annuity_matches_the_closed_form():
    rate, payment, years = 0.10, 100.0, 10
    flows = [0.0] + [payment] * years
    closed_form = payment * (1 - (1 + rate) ** -years) / rate
    assert finance.npv(rate, flows) == pytest.approx(closed_form)


def test_npv_at_zero_rate_is_the_plain_sum():
    flows = [-1000.0, 200.0, 300.0, 400.0]
    assert finance.npv(0.0, flows) == pytest.approx(-100.0)


def test_npv_rejects_a_rate_at_or_below_minus_one():
    with pytest.raises(finance.FinanceError):
        finance.npv(-1.0, [-100.0, 50.0])


def test_irr_zeroes_the_npv():
    flows = [-1000.0, 400.0, 400.0, 400.0]
    rate = finance.irr(flows)
    assert rate is not None
    assert finance.npv(rate, flows) == pytest.approx(0.0, abs=1e-6)


def test_irr_is_none_when_the_series_never_changes_sign():
    assert finance.irr([-100.0, -50.0, -25.0]) is None
    assert finance.irr([100.0, 50.0]) is None


def test_payback_interpolates_within_the_year():
    # 1000 out, 400 a year: cumulative crosses zero half way through year 3.
    flows = [-1000.0, 400.0, 400.0, 400.0, 400.0]
    assert finance.payback_years(flows) == pytest.approx(2.5)


def test_payback_is_none_when_it_never_pays_back():
    assert finance.payback_years([-1000.0, 10.0, 10.0]) is None


def test_discounted_payback_is_never_sooner_than_simple_payback():
    flows = [-1000.0, 300.0, 300.0, 300.0, 300.0, 300.0, 300.0]
    simple = finance.payback_years(flows)
    discounted = finance.payback_years(finance.discounted_cashflows(0.08, flows))
    assert discounted > simple


def test_lcoe_of_a_capex_only_project():
    # No opex, no discounting: cost per kWh is capex over lifetime output.
    value = finance.lcoe(10_000.0, [0.0] * 10, [1_000.0] * 10, discount_rate=0.0)
    assert value == pytest.approx(1.0)


def test_lcoe_rises_with_the_discount_rate():
    cheap = finance.lcoe(10_000.0, [100.0] * 25, [1_000.0] * 25, discount_rate=0.03)
    dear = finance.lcoe(10_000.0, [100.0] * 25, [1_000.0] * 25, discount_rate=0.12)
    assert dear > cheap


def test_lcoe_needs_output():
    with pytest.raises(finance.FinanceError):
        finance.lcoe(10_000.0, [0.0] * 5, [0.0] * 5, discount_rate=0.05)


# -- yearly series ----------------------------------------------------------


def test_degradation_compounds():
    series = finance.degraded_generation(1000.0, 1.0, 3)
    assert series == pytest.approx([1000.0, 990.0, 980.1])


def test_zero_degradation_is_flat():
    assert finance.degraded_generation(1000.0, 0.0, 5) == pytest.approx([1000.0] * 5)


def test_degradation_outside_zero_to_one_hundred_is_rejected():
    with pytest.raises(finance.FinanceError):
        finance.degraded_generation(1000.0, 120.0, 5)


def test_escalation_compounds_from_year_one():
    assert finance.escalated(100.0, 10.0, 3) == pytest.approx([100.0, 110.0, 121.0])


# -- capex ------------------------------------------------------------------


def test_capex_scales_with_capacity():
    assert finance.total_capex(balance(kwp=100.0), inputs()) == pytest.approx(90_000.0)
    assert finance.total_capex(balance(kwp=50.0), inputs()) == pytest.approx(45_000.0)


def test_lump_sums_add_on_top():
    got = finance.total_capex(
        balance(kwp=10.0),
        inputs(capex_per_kwp=1000.0, capex_lump_sums={"grid connection": 1500.0}),
    )
    assert got == pytest.approx(11_500.0)


def test_zero_capacity_cannot_be_costed():
    with pytest.raises(finance.FinanceError):
        finance.total_capex(balance(kwp=0.0), inputs())


# -- the gap rule -----------------------------------------------------------


def test_a_missing_gap_input_blocks_the_appraisal():
    incomplete = FinanceInputs(capex_per_kwp=900.0, import_tariff_per_kwh=0.30)
    assert "discount_rate" in incomplete.missing()
    assert "tariff_escalation" in incomplete.missing()
    with pytest.raises(finance.FinanceError) as excinfo:
        finance.build_cashflows(balance(), incomplete)
    assert "discount_rate" in str(excinfo.value)


def test_a_complete_input_set_reports_nothing_missing():
    assert inputs().missing() == []


# -- the whole appraisal ----------------------------------------------------


def test_cashflow_series_has_one_entry_per_year_plus_year_zero():
    flows = finance.build_cashflows(balance(), inputs(project_life_years=25))
    assert len(flows) == 26
    assert flows[0] < 0.0


def test_year_one_revenue_matches_the_energy_split():
    flows = finance.build_cashflows(
        balance(generation=100_000.0, self_consumed=70_000.0, exported=30_000.0),
        inputs(opex_per_year=0.0, tariff_escalation=0.0, degradation_pct_per_year=0.0),
    )
    assert flows[1] == pytest.approx(70_000.0 * 0.30 + 30_000.0 * 0.05)


def test_incentives_land_in_year_one_only():
    without = finance.build_cashflows(balance(), inputs())
    with_grant = finance.build_cashflows(balance(), inputs(incentives_year_one=5_000.0))
    assert with_grant[1] - without[1] == pytest.approx(5_000.0)
    assert with_grant[2] == pytest.approx(without[2])


def test_evaluate_returns_a_coherent_result():
    result = finance.evaluate(balance(), inputs())
    assert result.total_capex == pytest.approx(90_000.0)
    assert len(result.cashflows) == 26
    assert result.lcoe_per_kwh > 0.0
    assert result.currency == "GBP"
    assert result.npv == pytest.approx(finance.npv(0.07, result.cashflows))


def test_a_higher_tariff_improves_every_headline_metric():
    poor = finance.evaluate(balance(), inputs(import_tariff_per_kwh=0.10))
    rich = finance.evaluate(balance(), inputs(import_tariff_per_kwh=0.40))
    assert rich.npv > poor.npv
    assert rich.irr > poor.irr
    assert rich.simple_payback_years < poor.simple_payback_years


def test_a_higher_discount_rate_lowers_npv_but_not_irr():
    cheap = finance.evaluate(balance(), inputs(discount_rate=0.04))
    dear = finance.evaluate(balance(), inputs(discount_rate=0.12))
    assert dear.npv < cheap.npv
    assert dear.irr == pytest.approx(cheap.irr)


def test_lcoe_is_independent_of_the_tariff():
    low = finance.evaluate(balance(), inputs(import_tariff_per_kwh=0.10))
    high = finance.evaluate(balance(), inputs(import_tariff_per_kwh=0.50))
    assert low.lcoe_per_kwh == pytest.approx(high.lcoe_per_kwh)


def test_an_uneconomic_project_has_negative_npv_and_no_payback():
    result = finance.evaluate(
        balance(), inputs(capex_per_kwp=9_000.0, import_tariff_per_kwh=0.02,
                          export_tariff_per_kwh=0.0, opex_per_year=5_000.0)
    )
    assert result.npv < 0.0
    assert result.simple_payback_years is None


# -- carbon and MACC --------------------------------------------------------


def test_abatement_converts_kwh_to_tonnes():
    # 1000 MWh at 0.131 t/MWh.
    assert finance.abatement_tco2([1_000_000.0], 0.131) == pytest.approx(131.0)


def test_a_declining_grid_factor_abates_less():
    generation = [100_000.0] * 3
    fixed = finance.abatement_tco2(generation, 0.5)
    declining = finance.abatement_tco2_series(generation, [0.5, 0.4, 0.3])
    assert declining < fixed


def test_mismatched_carbon_series_are_rejected():
    with pytest.raises(finance.FinanceError):
        finance.abatement_tco2_series([1.0, 2.0], [0.5])


def test_macc_step_costs_per_tonne():
    step = MaccStep(label="PV tranche 1", delta_capex=90_000.0, delta_tco2=300.0)
    assert step.cost_per_tco2 == pytest.approx(300.0)


def test_a_step_that_abates_nothing_has_no_cost_per_tonne():
    assert MaccStep(label="nothing", delta_capex=1000.0, delta_tco2=0.0).cost_per_tco2 is None


def test_macc_bars_are_additive_and_contiguous():
    steps = [
        MaccStep(label="PV tranche 1", delta_capex=30_000.0, delta_tco2=200.0),
        MaccStep(label="PV tranche 2", delta_capex=32_000.0, delta_tco2=150.0),
        MaccStep(label="battery tranche 1", delta_capex=40_000.0, delta_tco2=60.0),
    ]
    bars = finance.macc_curve(steps)
    assert [b["label"] for b in bars] == ["PV tranche 1", "PV tranche 2", "battery tranche 1"]
    assert bars[0]["x_start"] == pytest.approx(0.0)
    for left, right in zip(bars, bars[1:]):
        assert left["x_end"] == pytest.approx(right["x_start"])
        assert left["cost_per_tco2"] <= right["cost_per_tco2"]
    assert bars[-1]["x_end"] == pytest.approx(410.0)


def test_macc_drops_steps_that_abate_nothing():
    steps = [
        MaccStep(label="real", delta_capex=1000.0, delta_tco2=10.0),
        MaccStep(label="null", delta_capex=1000.0, delta_tco2=0.0),
    ]
    assert [b["label"] for b in finance.macc_curve(steps)] == ["real"]


def test_macc_step_builder_uses_the_generation_increment():
    step = finance.macc_step(
        "PV tranche 2", delta_capex=20_000.0,
        delta_generation_kwh=[100_000.0] * 10, grid_factor_t_per_mwh=0.131,
    )
    assert step.delta_tco2 == pytest.approx(131.0)
    assert step.cost_per_tco2 == pytest.approx(20_000.0 / 131.0)


# -- battery capex -----------------------------------------------------------
#
# Regression: storage was priced at nothing, so a larger battery raised
# self-consumption and therefore NPV at no capital cost — wrong about exactly
# the decision the storage screen exists to inform.


def test_a_battery_costs_money():
    without = finance.total_capex(balance(), inputs())
    with_battery = finance.total_capex(
        balance(), inputs(battery_usable_kwh=50.0, battery_capex_per_kwh=500.0)
    )
    assert with_battery - without == pytest.approx(25_000.0)


def test_battery_capex_scales_with_usable_energy():
    small = finance.battery_capex(inputs(battery_usable_kwh=10.0, battery_capex_per_kwh=500.0))
    large = finance.battery_capex(inputs(battery_usable_kwh=100.0, battery_capex_per_kwh=500.0))
    assert large == pytest.approx(small * 10.0)


def test_no_battery_costs_nothing():
    assert finance.battery_capex(inputs()) == pytest.approx(0.0)


def test_a_battery_without_a_price_is_refused():
    """Silently pricing storage at zero is worse than refusing to compute."""
    with pytest.raises(finance.FinanceError) as excinfo:
        finance.battery_capex(inputs(battery_usable_kwh=50.0))
    assert "free" in str(excinfo.value)


def test_a_free_battery_can_no_longer_flatter_the_npv():
    priced = finance.evaluate(
        balance(), inputs(battery_usable_kwh=100.0, battery_capex_per_kwh=500.0)
    )
    unpriced = finance.evaluate(balance(), inputs())
    assert priced.npv < unpriced.npv


# -- mid-life replacement costs ---------------------------------------------


def test_a_replacement_cost_lands_in_its_own_year():
    plain = finance.build_cashflows(balance(), inputs())
    with_swap = finance.build_cashflows(balance(), inputs(year_costs={12: 7_000.0}))
    assert plain[12] - with_swap[12] == pytest.approx(7_000.0)
    assert with_swap[11] == pytest.approx(plain[11])
    assert with_swap[13] == pytest.approx(plain[13])


def test_a_replacement_cost_lowers_npv():
    assert (finance.evaluate(balance(), inputs(year_costs={12: 7_000.0})).npv
            < finance.evaluate(balance(), inputs()).npv)


def test_replacement_costs_reach_the_levelised_cost_too():
    """LCOE and the cashflow must not disagree about the same project."""
    assert (finance.evaluate(balance(), inputs(year_costs={12: 7_000.0})).lcoe_per_kwh
            > finance.evaluate(balance(), inputs()).lcoe_per_kwh)


# -- the abatement ladder ----------------------------------------------------


def test_pv_tranches_split_capex_and_output_evenly():
    tranches = finance.pv_tranches(total_capex=90_000.0, annual_kwh=90_000.0, count=3)
    assert len(tranches) == 3
    assert sum(t.delta_capex for t in tranches) == pytest.approx(90_000.0)
    assert sum(t.delta_annual_kwh for t in tranches) == pytest.approx(90_000.0)


def test_battery_tranches_are_additive_increments():
    tranches = finance.battery_tranches(
        sizes_kwh=[25.0, 50.0], recovered_kwh=[900.0, 400.0], capex_per_kwh=500.0
    )
    assert [t.delta_capex for t in tranches] == pytest.approx([12_500.0, 12_500.0])
    assert [t.delta_annual_kwh for t in tranches] == pytest.approx([900.0, 400.0])


def test_a_cleaning_tranche_is_all_opex_and_no_capex():
    tranche = finance.cleaning_tranche(100_000.0, soiling_recovered_pct=3.0,
                                       annual_cleaning_cost=2_000.0)
    assert tranche.delta_capex == pytest.approx(0.0)
    assert tranche.delta_annual_kwh == pytest.approx(3_000.0)
    assert tranche.delta_opex_per_year == pytest.approx(2_000.0)


def test_the_ladder_prices_recurring_opex_into_the_step():
    """A step whose cost is all opex must still be comparable with a capex step."""
    tranche = finance.cleaning_tranche(100_000.0, 3.0, 2_000.0)
    undiscounted = finance.macc_ladder([tranche], 0.131, 0.5, 25)[0]
    discounted = finance.macc_ladder([tranche], 0.131, 0.5, 25, discount_rate=0.07)[0]
    assert undiscounted.delta_capex == pytest.approx(50_000.0)   # 2,000 x 25
    assert 0.0 < discounted.delta_capex < undiscounted.delta_capex


def test_the_full_six_step_ladder_renders():
    tranches = (
        finance.pv_tranches(90_000.0, 90_000.0, 3)
        + finance.battery_tranches([25.0, 50.0], [900.0, 400.0], 500.0)
        + [finance.cleaning_tranche(90_000.0, 2.0, 1_500.0)]
    )
    steps = finance.macc_ladder(tranches, 0.131, 0.5, 25, discount_rate=0.07)
    bars = finance.macc_curve(steps)
    assert len(bars) == 6
    for left, right in zip(bars, bars[1:]):
        assert left["x_end"] == pytest.approx(right["x_start"])
        assert left["cost_per_tco2"] <= right["cost_per_tco2"]


def test_a_battery_that_rescues_nothing_abates_nothing():
    """Without an export limit there is no curtailment to recover, so the bar
    is empty — and an empty bar is dropped rather than drawn at zero."""
    tranches = finance.battery_tranches([25.0], [0.0], 500.0)
    steps = finance.macc_ladder(tranches, 0.131, 0.5, 25)
    assert steps[0].delta_tco2 == pytest.approx(0.0)
    assert steps[0].cost_per_tco2 is None
    assert finance.macc_curve(steps) == []


def test_mismatched_battery_series_are_rejected():
    with pytest.raises(finance.FinanceError):
        finance.battery_tranches([25.0, 50.0], [900.0], 500.0)

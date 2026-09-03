"""Battery dispatch and self-consumption."""

from __future__ import annotations

import math

import pytest

from arka import dispatch
from arka.scenario import BatterySpec


NO_BATTERY = BatterySpec(usable_kwh=0.0, power_kw=0.0)


# -- energy conservation ----------------------------------------------------


def test_without_a_battery_every_kwh_is_accounted_for(flat_generation, flat_load):
    result = dispatch.simulate(flat_generation, flat_load, NO_BATTERY)
    assert result.generation_kwh == pytest.approx(sum(flat_generation))
    assert result.self_consumed_kwh + result.exported_kwh == pytest.approx(sum(flat_generation))
    assert result.self_consumed_kwh + result.imported_kwh == pytest.approx(sum(flat_load))
    assert result.charged_kwh == pytest.approx(0.0)


def test_with_a_battery_generation_still_balances(flat_generation, flat_load):
    battery = BatterySpec(usable_kwh=20.0, power_kw=10.0, round_trip_efficiency=0.9)
    result = dispatch.simulate(flat_generation, flat_load, battery)
    assert result.generation_kwh == pytest.approx(sum(flat_generation))


def test_load_balances_against_supply(flat_generation, flat_load):
    battery = BatterySpec(usable_kwh=20.0, power_kw=10.0, round_trip_efficiency=0.9)
    result = dispatch.simulate(flat_generation, flat_load, battery)
    served = result.self_consumed_kwh + result.discharged_kwh + result.imported_kwh
    assert served == pytest.approx(sum(flat_load))


# -- efficiency -------------------------------------------------------------

def test_round_trip_losses_are_real():
    generation = [10.0, 0.0]
    load = [0.0, 10.0]
    battery = BatterySpec(usable_kwh=100.0, power_kw=100.0, round_trip_efficiency=0.81)
    result = dispatch.simulate(generation, load, battery)
    assert result.charged_kwh == pytest.approx(10.0)
    # 81% round trip means 8.1 kWh comes back out of 10 kWh in.
    assert result.discharged_kwh == pytest.approx(8.1)
    assert result.imported_kwh == pytest.approx(1.9)


def test_a_lossless_battery_returns_everything():
    generation = [10.0, 0.0]
    load = [0.0, 10.0]
    battery = BatterySpec(usable_kwh=100.0, power_kw=100.0, round_trip_efficiency=1.0)
    result = dispatch.simulate(generation, load, battery)
    assert result.discharged_kwh == pytest.approx(10.0)
    assert result.imported_kwh == pytest.approx(0.0)


# -- limits -----------------------------------------------------------------


def test_power_rating_caps_charge_and_discharge():
    generation = [10.0, 0.0]
    load = [0.0, 10.0]
    battery = BatterySpec(usable_kwh=100.0, power_kw=3.0, round_trip_efficiency=1.0)
    result = dispatch.simulate(generation, load, battery)
    assert result.charged_kwh == pytest.approx(3.0)
    assert result.discharged_kwh == pytest.approx(3.0)
    assert result.exported_kwh == pytest.approx(7.0)


def test_capacity_caps_the_stored_energy():
    generation = [10.0, 10.0, 0.0]
    load = [0.0, 0.0, 20.0]
    battery = BatterySpec(usable_kwh=5.0, power_kw=100.0, round_trip_efficiency=1.0)
    result = dispatch.simulate(generation, load, battery)
    assert result.charged_kwh == pytest.approx(5.0)
    assert result.exported_kwh == pytest.approx(15.0)


def test_minimum_state_of_charge_is_held_back():
    generation = [10.0, 0.0]
    load = [0.0, 10.0]
    battery = BatterySpec(
        usable_kwh=10.0, power_kw=100.0, round_trip_efficiency=1.0, min_soc_fraction=0.2
    )
    result = dispatch.simulate(generation, load, battery)
    assert result.discharged_kwh == pytest.approx(8.0)


def test_export_limit_curtails_the_surplus():
    generation = [10.0]
    load = [0.0]
    result = dispatch.simulate(generation, load, NO_BATTERY, export_limit_kw=4.0)
    assert result.exported_kwh == pytest.approx(4.0)
    assert result.curtailed_kwh == pytest.approx(6.0)
    assert result.generation_kwh == pytest.approx(10.0)


# -- self-consumption -------------------------------------------------------


def test_a_battery_raises_self_consumption(flat_generation, flat_load):
    without = dispatch.simulate(flat_generation, flat_load, NO_BATTERY)
    with_battery = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=30.0, power_kw=15.0, round_trip_efficiency=0.9),
    )
    assert with_battery.self_consumption_fraction > without.self_consumption_fraction
    assert with_battery.imported_kwh < without.imported_kwh


def test_self_consumption_fraction_never_exceeds_one(flat_generation, flat_load):
    result = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=500.0, power_kw=500.0, round_trip_efficiency=0.9),
    )
    assert 0.0 <= result.self_consumption_fraction <= 1.0


def test_cycles_count_charge_throughput():
    generation = [10.0, 0.0, 10.0, 0.0]
    load = [0.0, 10.0, 0.0, 10.0]
    battery = BatterySpec(usable_kwh=10.0, power_kw=100.0, round_trip_efficiency=1.0)
    result = dispatch.simulate(generation, load, battery)
    assert result.cycles == pytest.approx(2.0)


# -- sweeps -----------------------------------------------------------------


def test_sweep_is_monotonic_in_self_consumption(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 5.0, 10.0, 20.0, 40.0])
    fractions = [p.self_consumption_fraction for p in points]
    assert fractions == sorted(fractions)


def test_sweep_shows_diminishing_returns(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    marginal = [shift for _, shift in dispatch.marginal_shift(points)]
    # Each additional tranche shifts no more energy than the one before it.
    for earlier, later in zip(marginal[1:], marginal[2:]):
        assert later <= earlier + 1e-6


def test_marginal_shift_sums_to_the_largest_battery(flat_generation, flat_load):
    sizes = [0.0, 10.0, 20.0, 40.0]
    points = dispatch.sweep(flat_generation, flat_load, sizes)
    total = sum(shift for _, shift in dispatch.marginal_shift(points))
    assert total == pytest.approx(points[-1].result.discharged_kwh)


# -- load profiles ----------------------------------------------------------


def test_synthetic_profile_hits_its_annual_total():
    profile = dispatch.synthetic_load_profile(120_000.0, shape="office")
    assert len(profile.hourly_kwh) == 8760
    assert profile.annual_kwh == pytest.approx(120_000.0)


def test_synthetic_profile_is_labelled_as_synthetic():
    profile = dispatch.synthetic_load_profile(1000.0, shape="residential")
    assert profile.label.startswith("synthetic:")


def test_weekend_factor_reshapes_without_changing_the_total():
    quiet = dispatch.synthetic_load_profile(100_000.0, shape="office", weekend_factor=0.2)
    busy = dispatch.synthetic_load_profile(100_000.0, shape="office", weekend_factor=1.0)
    assert quiet.annual_kwh == pytest.approx(busy.annual_kwh)
    assert quiet.hourly_kwh != busy.hourly_kwh


def test_office_and_residential_shapes_differ_where_it_matters():
    office = dispatch.synthetic_load_profile(8760.0, shape="office").hourly_kwh
    home = dispatch.synthetic_load_profile(8760.0, shape="residential").hourly_kwh
    # Midday: an office is at its peak, a home is not.
    assert office[12] > home[12]
    # Evening: the other way round.
    assert home[19] > office[19]


@pytest.mark.parametrize("shape", dispatch.shapes())
def test_every_shape_produces_a_full_year(shape):
    profile = dispatch.synthetic_load_profile(5000.0, shape=shape)
    assert len(profile.hourly_kwh) == 8760
    assert all(math.isfinite(v) and v >= 0.0 for v in profile.hourly_kwh)


# -- rejected inputs --------------------------------------------------------


def test_mismatched_series_lengths_are_rejected():
    with pytest.raises(dispatch.DispatchError):
        dispatch.simulate([1.0, 2.0], [1.0], NO_BATTERY)


def test_empty_series_is_rejected():
    with pytest.raises(dispatch.DispatchError):
        dispatch.simulate([], [], NO_BATTERY)


def test_impossible_efficiency_is_rejected():
    with pytest.raises(dispatch.DispatchError):
        dispatch.simulate([1.0], [1.0], BatterySpec(usable_kwh=1.0, round_trip_efficiency=1.5))


def test_unknown_load_shape_is_rejected():
    with pytest.raises(dispatch.DispatchError):
        dispatch.synthetic_load_profile(1000.0, shape="nonexistent")


# -- curtailment recovery ----------------------------------------------------
#
# The carbon side of the MACC needs energy rescued from curtailment, not energy
# moved from export to self-consumption: exported energy displaces grid
# generation either way, so counting the shift would double-count abatement
# already credited to the PV tranches.


def test_without_an_export_limit_a_battery_rescues_nothing(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 20.0, 40.0])
    assert all(kwh == pytest.approx(0.0)
               for _, kwh in dispatch.marginal_curtailment_recovered(points))


def test_with_an_export_limit_a_battery_rescues_curtailed_energy(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 20.0, 40.0], export_limit_kw=2.0)
    recovered = dispatch.marginal_curtailment_recovered(points)
    assert recovered[0][1] == pytest.approx(0.0)      # baseline rescues nothing
    assert recovered[1][1] > 0.0                       # first tranche does


def test_curtailment_recovery_shows_diminishing_returns(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 10.0, 20.0, 30.0, 40.0],
                            export_limit_kw=2.0)
    marginal = [kwh for _, kwh in dispatch.marginal_curtailment_recovered(points)][1:]
    for earlier, later in zip(marginal, marginal[1:]):
        assert later <= earlier + 1e-6


def test_recovered_energy_never_exceeds_what_was_curtailed(flat_generation, flat_load):
    points = dispatch.sweep(flat_generation, flat_load, [0.0, 40.0], export_limit_kw=2.0)
    total_recovered = sum(kwh for _, kwh in dispatch.marginal_curtailment_recovered(points))
    assert total_recovered <= points[0].result.curtailed_kwh + 1e-6


# -- curtailment reserve -----------------------------------------------------
#
# Charging greedily from any surplus fills the pack in the morning, leaving it
# full when clipping actually happens at midday. That made small batteries look
# useless against an export limit and produced increasing marginal returns.


def test_the_reserve_is_inert_without_an_export_limit(flat_generation, flat_load):
    plain = dispatch.simulate(flat_generation, flat_load,
                              BatterySpec(usable_kwh=20.0, power_kw=10.0))
    reserved = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=20.0, power_kw=10.0, curtailment_reserve_fraction=0.5),
    )
    # With nothing being clipped, every kWh of surplus counts as clipped and has
    # first call on the whole pack, so behaviour is unchanged.
    assert reserved.self_consumed_kwh == pytest.approx(plain.self_consumed_kwh)
    assert reserved.charged_kwh == pytest.approx(plain.charged_kwh)


def test_a_reserve_cuts_curtailment_under_an_export_limit(flat_generation, flat_load):
    greedy = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=20.0, power_kw=10.0), export_limit_kw=2.0,
    )
    reserved = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=20.0, power_kw=10.0, curtailment_reserve_fraction=0.5),
        export_limit_kw=2.0,
    )
    assert reserved.curtailed_kwh <= greedy.curtailed_kwh


def test_clipped_energy_is_charged_before_ordinary_surplus():
    """The pack should absorb what is about to be thrown away first."""
    generation, load = [10.0], [0.0]
    battery = BatterySpec(usable_kwh=4.0, power_kw=4.0, round_trip_efficiency=1.0,
                          curtailment_reserve_fraction=1.0)
    result = dispatch.simulate(generation, load, battery, export_limit_kw=7.0)
    # 3 kWh would be clipped; the whole pack is reserved for exactly that.
    assert result.charged_kwh == pytest.approx(3.0)
    assert result.curtailed_kwh == pytest.approx(0.0)
    assert result.exported_kwh == pytest.approx(7.0)


def test_a_full_reserve_refuses_ordinary_surplus():
    generation, load = [5.0], [0.0]
    battery = BatterySpec(usable_kwh=10.0, power_kw=10.0, round_trip_efficiency=1.0,
                          curtailment_reserve_fraction=1.0)
    result = dispatch.simulate(generation, load, battery, export_limit_kw=20.0)
    assert result.charged_kwh == pytest.approx(0.0)   # nothing was at risk
    assert result.exported_kwh == pytest.approx(5.0)


def test_energy_still_balances_with_a_reserve(flat_generation, flat_load):
    result = dispatch.simulate(
        flat_generation, flat_load,
        BatterySpec(usable_kwh=30.0, power_kw=15.0, curtailment_reserve_fraction=0.4),
        export_limit_kw=3.0,
    )
    assert result.generation_kwh == pytest.approx(sum(flat_generation))

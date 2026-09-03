"""Market incentive schemes, computed from the cited slab structure."""

from __future__ import annotations

import pytest

from arka import benchmarks, incentives


@pytest.fixture(scope="module")
def table():
    return benchmarks.load()


# -- PM Surya Ghar: the slab structure and its cap ---------------------------


def test_first_two_kw_pay_the_top_rate(table):
    result = incentives.pm_surya_ghar(2.0, table)
    assert result.amount == pytest.approx(60_000.0)   # 2 kW x 30,000
    assert result.currency == "INR"


def test_the_third_kw_pays_a_lower_rate(table):
    result = incentives.pm_surya_ghar(3.0, table)
    assert result.amount == pytest.approx(78_000.0)   # 60,000 + 18,000


def test_a_partial_first_slab_is_prorated(table):
    assert incentives.pm_surya_ghar(1.0, table).amount == pytest.approx(30_000.0)
    assert incentives.pm_surya_ghar(0.5, table).amount == pytest.approx(15_000.0)


def test_the_cap_binds_above_three_kw(table):
    """The kink the CSV calls the biggest in Indian residential sizing economics.

    A 5 kWp system receives exactly what a 3 kWp system receives.
    """
    three = incentives.pm_surya_ghar(3.0, table)
    five = incentives.pm_surya_ghar(5.0, table)
    ten = incentives.pm_surya_ghar(10.0, table)
    assert five.amount == pytest.approx(three.amount)
    assert ten.amount == pytest.approx(three.amount)
    assert five.capped


def test_the_cap_is_explained_not_just_applied(table):
    result = incentives.pm_surya_ghar(5.0, table)
    assert "Nothing above 3 kW" in result.explain()
    assert result.sources


def test_a_zero_size_array_earns_nothing(table):
    assert incentives.pm_surya_ghar(0.0, table).amount == pytest.approx(0.0)


def test_subsidy_per_kwp_falls_once_the_cap_binds(table):
    """The economic consequence: marginal support goes to zero."""
    per_kwp = lambda k: incentives.pm_surya_ghar(k, table).amount / k  # noqa: E731
    assert per_kwp(3.0) > per_kwp(6.0) > per_kwp(12.0)


# -- UK Annual Investment Allowance -----------------------------------------


def test_aia_is_a_percentage_of_capex(table):
    result = incentives.uk_annual_investment_allowance(100_000.0, table)
    assert result.amount == pytest.approx(22_000.0)   # central band, 22%
    assert result.currency == "GBP"


def test_aia_bands_bracket_the_central_value(table):
    low = incentives.uk_annual_investment_allowance(100_000.0, table, band="low").amount
    high = incentives.uk_annual_investment_allowance(100_000.0, table, band="high").amount
    assert low == pytest.approx(19_000.0)
    assert high == pytest.approx(25_000.0)
    assert low < high


def test_aia_on_no_capex_is_nothing(table):
    assert incentives.uk_annual_investment_allowance(0.0, table).amount == pytest.approx(0.0)


# -- market routing ----------------------------------------------------------


def test_each_market_routes_to_its_own_scheme(table):
    assert "Surya Ghar" in incentives.for_market("India", 3.0, 200_000.0, table).scheme
    assert "Investment Allowance" in incentives.for_market("UK", 100.0, 90_000.0, table).scheme


def test_uae_has_no_capital_scheme_and_says_so(table):
    """Not a gap: the UAE incentive is net metering, which shows up in the
    energy balance rather than as a cash inflow."""
    result = incentives.for_market("UAE", 100.0, 200_000.0, table)
    assert result.amount == pytest.approx(0.0)
    assert result.scheme == "none"


def test_market_routing_is_case_insensitive(table):
    assert incentives.for_market("india", 3.0, 0.0, table).amount > 0.0
    assert incentives.for_market("uk", 0.0, 100_000.0, table).amount > 0.0

"""The benchmark CSV, and the rule that gaps stay empty."""

from __future__ import annotations

import pytest

from arka import benchmarks


@pytest.fixture(scope="module")
def table() -> benchmarks.BenchmarkTable:
    return benchmarks.load()


def test_the_shipped_csv_loads(table):
    assert len(table) > 0


def test_every_row_has_a_recognised_tier(table):
    for row in table:
        assert isinstance(row.source_tier, benchmarks.Tier)


def test_non_gap_rows_carry_a_source(table):
    for row in table:
        if row.source_tier in (benchmarks.Tier.GAP, benchmarks.Tier.CONVENTION):
            continue
        assert row.source, f"{row.market}/{row.parameter} has a value but no source"
        assert row.source_url, f"{row.market}/{row.parameter} has a source but no URL"


def test_market_filter_includes_cross_market_rows(table):
    uk = table.for_market("UK")
    markets = {row.market for row in uk}
    assert markets == {"UK", "all"}


def test_market_filter_can_exclude_cross_market_rows(table):
    uk_only = table.for_market("UK", include_all=False)
    assert {row.market for row in uk_only} == {"UK"}


def test_market_lookup_is_case_insensitive(table):
    assert len(table.for_market("uk")) == len(table.for_market("UK"))
    assert len(table.for_market("india")) == len(table.for_market("India"))


def test_a_known_authoritative_figure_reads_back(table):
    row = table.find("Grid emission factor (DEFRA 2026)", market="UK")
    assert row.value() == pytest.approx(0.131)
    assert row.source_tier is benchmarks.Tier.AUTHORITATIVE
    assert row.unit == "kgCO2e_per_kWh"


def test_currency_comes_from_the_market(table):
    assert table.currency_for("UK") == "GBP"
    assert table.currency_for("India") == "INR"
    assert table.currency_for("UAE") == "AED"


# -- the gap rule -----------------------------------------------------------


def test_the_csv_still_declares_gaps(table):
    gaps = table.gaps()
    assert gaps, "the gap rows are load-bearing; something has silently filled them in"
    names = {row.parameter for row in gaps}
    assert "Discount rate" in names
    assert "Tariff escalation" in names


def test_reading_a_gap_raises_rather_than_guessing(table):
    row = table.find("Discount rate")
    assert row.is_gap
    with pytest.raises(benchmarks.GapError):
        row.value()


def test_a_gap_can_be_filled_by_an_override(table):
    assert table.get("Discount rate", override=0.07) == pytest.approx(0.07)


def test_getting_a_gap_without_an_override_raises(table):
    with pytest.raises(benchmarks.GapError):
        table.get("Tariff escalation")


def test_gap_rows_carry_guidance(table):
    for row in table.gaps():
        assert row.remarks, f"gap row {row.parameter!r} must say what the user should supply"


# -- bands ------------------------------------------------------------------


def test_bands_are_ordered(table):
    for row in table:
        if row.is_gap or row.low is None or row.high is None:
            continue
        assert row.low <= row.central <= row.high, f"{row.market}/{row.parameter} bands are out of order"


def test_low_and_high_bands_read_back(table):
    row = table.find("PV installed cost commercial 50-250 kWp", market="UK")
    assert row.value(benchmarks.Band.LOW) == pytest.approx(750.0)
    assert row.value(benchmarks.Band.CENTRAL) == pytest.approx(900.0)
    assert row.value(benchmarks.Band.HIGH) == pytest.approx(1050.0)


# -- lookup behaviour -------------------------------------------------------


def test_an_unknown_parameter_raises(table):
    with pytest.raises(benchmarks.BenchmarkNotFound):
        table.find("cost of a unicorn", market="UK")


def test_substring_lookup_finds_a_row(table):
    row = table.find("Module degradation")
    assert row.market == "all"


def test_lookup_prefers_the_more_authoritative_row(table):
    row = table.find("Grid emission factor", market="India")
    assert row.source_tier is benchmarks.Tier.AUTHORITATIVE


# -- assumptions table ------------------------------------------------------


def test_assumptions_carry_the_tier_to_the_ui(table):
    rows = table.assumptions("UK")
    assert rows
    assert all("tier" in row and row["tier"] for row in rows)
    assert all("source" in row for row in rows)


def test_assumptions_show_gaps_as_blank_not_zero(table):
    rows = {r["parameter"]: r for r in table.assumptions()}
    assert rows["Discount rate"]["value"] == "—"


def test_assumptions_lead_with_the_most_authoritative(table):
    tiers = [row["tier"] for row in table.assumptions("UK")]
    ranks = [benchmarks.Tier(t).rank for t in tiers]
    assert ranks == sorted(ranks)


def test_every_tier_explains_itself():
    for tier in benchmarks.Tier:
        assert tier.caveat


# -- lookup robustness -------------------------------------------------------
#
# Found live: a model calling the benchmark tool phrases parameter names in
# snake_case ("grid_emission_factor") while the CSV uses prose with punctuation
# ("Grid emission factor (DEFRA 2026)"). Normalisation has to bridge that or the
# agent's first tool call fails.


def test_normalise_folds_underscores_and_hyphens():
    assert benchmarks._normalise("grid_emission_factor") == "grid emission factor"
    assert benchmarks._normalise("Market-Survey  Row") == "market survey row"


def test_snake_case_parameter_names_resolve(table):
    row = table.find("grid_emission_factor", market="UK")
    assert row.value() == pytest.approx(0.131)


def test_snake_case_gap_lookup_still_reports_a_gap(table):
    assert table.find("discount_rate").is_gap


def test_token_overlap_finds_a_row_with_extra_words(table):
    # Every token present, but not as a contiguous substring.
    row = table.find("module degradation")
    assert row.parameter == "Module degradation"


def test_a_missed_lookup_names_what_is_available(table):
    with pytest.raises(benchmarks.BenchmarkNotFound) as excinfo:
        table.find("cost of a unicorn", market="UK")
    assert "Available:" in str(excinfo.value)

"""The guard that keeps model-invented figures out of the report.

Rule 1 of CLAUDE.md is the one that is easiest to violate by accident, so it is
tested harder than anything else in the agent layer. No API key is needed:
nothing here calls the SDK.
"""

from __future__ import annotations

import pytest

from arka.agent import client, tools


# -- numeral extraction -----------------------------------------------------


def test_numerals_are_found_including_thousands_separators():
    found = client.numerals("The array is 211.2 kWp and yields 198,450 kWh.")
    assert "211.2" in found
    assert "198,450" in found


def test_prose_without_numbers_yields_nothing():
    assert client.numerals("The roof faces broadly south.") == []


# -- collecting the computed values -----------------------------------------


def test_values_are_collected_from_nested_results():
    payload = {"kwp": 211.2, "monthly": [10.0, 20.0], "meta": {"rows": 12}}
    values = client.collect_values(payload)
    assert 211.2 in values and 20.0 in values and 12.0 in values


def test_booleans_are_not_treated_as_numbers():
    assert client.collect_values({"ok": True, "also": False}) == []


def test_numbers_inside_strings_are_collected():
    values = client.collect_values({"source": "PVGIS 5.3, representative year 2019"})
    assert 5.3 in values


# -- the guard itself -------------------------------------------------------


def test_a_quoted_figure_passes():
    text = "The 211.2 kWp array generates 198450 kWh a year."
    assert client.verify_no_invented_numbers(text, [211.2, 198450.0]) == []


def test_an_invented_figure_is_caught():
    text = "The array generates 198450 kWh and saves 42000 pounds."
    offenders = client.verify_no_invented_numbers(text, [198450.0])
    assert offenders == ["42000"]


def test_rounding_for_presentation_is_allowed():
    text = "Annual output is 198,000 kWh."
    assert client.verify_no_invented_numbers(text, [198_450.0]) == []


def test_rounding_beyond_tolerance_is_caught():
    text = "Annual output is 250,000 kWh."
    assert client.verify_no_invented_numbers(text, [198_450.0]) == ["250,000"]


def test_unit_rescaling_is_allowed():
    # 198,450 kWh shown as 198.45 MWh.
    assert client.verify_no_invented_numbers("That is 198.45 MWh.", [198_450.0]) == []


def test_dates_and_small_counts_are_ignored():
    text = "Using the 2026 DEFRA factor, across 3 tranches and 12 months."
    assert client.verify_no_invented_numbers(text, []) == []


def test_a_project_life_must_still_trace_to_a_source():
    # 25 years is a modelling convention row in the CSV, not something the
    # model may assert on its own.
    assert client.verify_no_invented_numbers("Over 25 years, ...", []) == ["25"]
    assert client.verify_no_invented_numbers("Over 25 years, ...", [25.0]) == []


def test_an_empty_computed_set_still_catches_a_quantity():
    assert client.verify_no_invented_numbers("Payback is 7.4 years.", []) == ["7.4"]


def test_prose_with_no_numbers_always_passes():
    assert client.verify_no_invented_numbers("The roof is suitable.", []) == []


def test_the_guard_error_names_the_offenders():
    error = client.NumberGuardError(["42000", "7.4"])
    assert "42000" in str(error)
    assert error.offenders == ["42000", "7.4"]


# -- turn bookkeeping -------------------------------------------------------


def test_a_turn_exposes_every_computed_value():
    turn = client.Turn(
        text="",
        tool_results=[{"kwp": 100.0}, {"annual_kwh": 95_000.0, "monthly": [8000.0]}],
    )
    values = turn.computed_values
    assert 100.0 in values and 95_000.0 in values and 8000.0 in values


# -- the tool registry ------------------------------------------------------


def test_every_registered_tool_is_callable():
    assert tools.REGISTRY
    for name, fn in tools.REGISTRY.items():
        assert callable(fn), name
        assert fn.__doc__, f"{name} needs a docstring; the SDK builds its schema from it"


def test_tool_declarations_match_the_registry():
    assert len(client.tool_declarations()) == len(tools.REGISTRY)


def test_an_unknown_tool_is_rejected():
    with pytest.raises(KeyError):
        tools.call("delete_everything", {})


def test_polygon_metrics_runs_without_a_key():
    result = tools.polygon_metrics(
        [[-0.12, 51.5], [-0.1197, 51.5], [-0.1197, 51.5002], [-0.12, 51.5002]]
    )
    assert result["gross_area_m2"] > 0.0
    assert 0.0 <= result["implied_azimuth_deg"] < 360.0


def test_benchmark_lookup_reports_a_gap_rather_than_a_number():
    result = tools.lookup_benchmark("Discount rate", market="UK")
    assert result["value"] is None
    assert result["source_tier"] == "gap"
    assert result["gap_reason"]


def test_benchmark_lookup_returns_a_cited_value():
    result = tools.lookup_benchmark("Grid emission factor (DEFRA 2026)", market="UK")
    assert result["value"] == pytest.approx(0.131)
    assert result["source_url"].startswith("http")


def test_gaps_are_listed_for_a_market():
    result = tools.list_gaps("India")
    assert result["gaps"]
    assert all(gap["guidance"] for gap in result["gaps"])


def test_appraise_returns_computed_figures():
    result = tools.appraise(
        kwp=100.0, annual_generation_kwh=100_000.0, self_consumed_kwh=70_000.0,
        exported_kwh=30_000.0, capex_per_kwp=900.0, import_tariff_per_kwh=0.30,
        discount_rate_pct=7.0, tariff_escalation_pct=2.0, project_life_years=25,
        degradation_pct_per_year=0.5,
    )
    assert result["total_capex"] == pytest.approx(90_000.0)
    assert result["lcoe_per_kwh"] > 0.0


# -- the client without a key -----------------------------------------------


def test_the_agent_reports_itself_unavailable_without_a_key(monkeypatch):
    monkeypatch.delenv(client.API_KEY_ENV, raising=False)
    assert client.ArkaAgent().available is False


def test_calling_the_agent_without_a_key_explains_itself(monkeypatch):
    monkeypatch.delenv(client.API_KEY_ENV, raising=False)
    with pytest.raises(client.AgentError) as excinfo:
        client.ArkaAgent().parse_intake("a 500 square metre warehouse roof in Dubai")
    assert client.API_KEY_ENV in str(excinfo.value)


def test_the_system_instruction_states_the_rule():
    assert "never produce a number" in client.SYSTEM_INSTRUCTION.lower()


# -- intake schema ----------------------------------------------------------


def test_the_intake_schema_carries_no_energy_or_currency_fields():
    from arka.agent.schema import ArrayIntake, SiteIntake

    banned = ("kwh", "kwp", "npv", "tco2", "cost", "capex", "yield", "generation")
    for model in (SiteIntake, ArrayIntake):
        for name in model.model_fields:
            assert not any(token in name.lower() for token in banned), (
                f"{model.__name__}.{name} would let the model state a figure"
            )


# -- schema compatibility with Gemini ---------------------------------------
#
# Found live: Gemini's schema converter rejects exclusiveMinimum/exclusiveMaximum,
# so a Pydantic `lt=`/`gt=` constraint makes every structured-output call fail
# with a ValidationError before any request is sent.


def test_no_intake_field_uses_an_exclusive_bound():
    from arka.agent.schema import ArrayIntake, FinanceIntake, Intake, SiteIntake

    for model in (SiteIntake, ArrayIntake, FinanceIntake, Intake):
        for name, field in model.model_fields.items():
            for meta in field.metadata:
                kind = type(meta).__name__
                assert kind not in ("Lt", "Gt"), (
                    f"{model.__name__}.{name} uses an exclusive bound ({kind}); "
                    "Gemini's schema converter rejects exclusiveMinimum/exclusiveMaximum"
                )


def test_azimuth_360_wraps_to_zero():
    from arka.agent.schema import ArrayIntake

    assert ArrayIntake(azimuth_deg=360.0).azimuth_deg == pytest.approx(0.0)
    assert ArrayIntake(azimuth_deg=180.0).azimuth_deg == pytest.approx(180.0)
    assert ArrayIntake(azimuth_deg=None).azimuth_deg is None


# -- tool robustness ---------------------------------------------------------
#
# Found live: the model guesses parameter names. A miss must hand it the valid
# names to retry with, not raise and discard the whole turn.


def test_an_unmatched_benchmark_returns_choices_instead_of_raising():
    result = tools.lookup_benchmark("cost of a unicorn", market="UK")
    assert result["value"] is None
    assert result["error"]
    assert "Grid emission factor (DEFRA 2026)" in result["available_parameters"]


def test_automatic_function_calling_is_disabled():
    """The SDK must not execute tools itself.

    If it did, results would never pass through `tools.call`, `Turn.tool_results`
    would be empty, and the number guard would flag every legitimate figure as
    invented. This asserts the config we send, not the SDK's behaviour.
    """
    agent = client.ArkaAgent(api_key="not-a-real-key")
    config = agent._config(with_tools=True)
    assert config.automatic_function_calling.disable is True

"""Appraisal arithmetic: NPV, IRR, payback, LCOE and the abatement curve.

Every input arrives as an argument. Nothing here reads the benchmark CSV — the
caller pulls figures out of `benchmarks.py` and passes them in, so a change of
market never changes the maths.

Sign convention: year 0 is the capex outflow, years 1..N are net benefit
inflows. Cashflow index equals year number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy_financial as npf

from .scenario import FinanceInputs, FinanceResult, MaccStep


class FinanceError(ValueError):
    """The appraisal cannot be run with the inputs given."""


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def npv(rate: float, cashflows: list[float]) -> float:
    """Net present value with cashflows[0] at t=0, undiscounted."""
    if rate <= -1.0:
        raise FinanceError("discount rate must be greater than -100%")
    return float(sum(cf / (1.0 + rate) ** t for t, cf in enumerate(cashflows)))


def irr(cashflows: list[float]) -> float | None:
    """Internal rate of return, or None when the series has no sign change."""
    if not cashflows:
        return None
    signs = {cf > 0.0 for cf in cashflows if not math.isclose(cf, 0.0, abs_tol=1e-12)}
    if len(signs) < 2:
        return None
    value = npf.irr(cashflows)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value)


def cumulative(cashflows: list[float]) -> list[float]:
    running = 0.0
    out = []
    for cf in cashflows:
        running += cf
        out.append(running)
    return out


def payback_years(cashflows: list[float]) -> float | None:
    """First crossing of zero cumulative cash, linearly interpolated within the year."""
    running = cumulative(cashflows)
    for year, value in enumerate(running):
        if value >= 0.0:
            if year == 0:
                return 0.0
            previous = running[year - 1]
            step = value - previous
            if math.isclose(step, 0.0, abs_tol=1e-12):
                return float(year)
            return (year - 1) + (-previous / step)
    return None


def discounted_cashflows(rate: float, cashflows: list[float]) -> list[float]:
    return [cf / (1.0 + rate) ** t for t, cf in enumerate(cashflows)]


def lcoe(
    total_capex: float,
    annual_opex: list[float],
    annual_generation_kwh: list[float],
    discount_rate: float,
) -> float:
    """Levelised cost of energy: discounted lifetime cost over discounted output.

    Opex and generation are year-1-onward lists of equal length.
    """
    if len(annual_opex) != len(annual_generation_kwh):
        raise FinanceError("opex and generation series must be the same length")
    discounted_cost = total_capex + sum(
        cost / (1.0 + discount_rate) ** (t + 1) for t, cost in enumerate(annual_opex)
    )
    discounted_energy = sum(
        kwh / (1.0 + discount_rate) ** (t + 1) for t, kwh in enumerate(annual_generation_kwh)
    )
    if discounted_energy <= 0.0:
        raise FinanceError("no discounted generation; cannot levelise a cost over zero output")
    return discounted_cost / discounted_energy


# ---------------------------------------------------------------------------
# Yearly series
# ---------------------------------------------------------------------------


def degraded_generation(year_one_kwh: float, degradation_pct_per_year: float,
                        life_years: int) -> list[float]:
    """Annual output over the project life, compounding module degradation."""
    if life_years <= 0:
        raise FinanceError("project life must be at least one year")
    factor = 1.0 - degradation_pct_per_year / 100.0
    if not 0.0 < factor <= 1.0:
        raise FinanceError("degradation must be between 0 and 100 percent per year")
    return [year_one_kwh * factor ** t for t in range(life_years)]


def escalated(value: float, escalation_pct_per_year: float, life_years: int) -> list[float]:
    """A value escalating annually from year 1."""
    factor = 1.0 + escalation_pct_per_year / 100.0
    return [value * factor ** t for t in range(life_years)]


@dataclass(frozen=True)
class EnergyBalance:
    """Year-one energy split, straight out of the dispatch model."""

    kwp: float
    generation_kwh: float
    self_consumed_kwh: float
    exported_kwh: float

    @property
    def offset_kwh(self) -> float:
        """Energy that displaces an import — the part valued at the retail tariff."""
        return self.self_consumed_kwh


def build_cashflows(balance: EnergyBalance, inputs: FinanceInputs) -> list[float]:
    """Year 0..N net cashflow series in the market currency."""
    missing = inputs.missing()
    if missing:
        raise FinanceError(
            "cannot build a cashflow without user inputs: " + ", ".join(missing)
            + ". These are 'gap'-tiered — they have no defensible default."
        )
    life = int(inputs.project_life_years)
    capex = total_capex(balance, inputs)

    generation = degraded_generation(balance.generation_kwh, inputs.degradation_pct_per_year, life)
    offset_share = (
        balance.offset_kwh / balance.generation_kwh if balance.generation_kwh > 0.0 else 0.0
    )
    export_share = (
        balance.exported_kwh / balance.generation_kwh if balance.generation_kwh > 0.0 else 0.0
    )
    import_tariff = escalated(inputs.import_tariff_per_kwh, inputs.tariff_escalation, life)
    export_tariff = escalated(inputs.export_tariff_per_kwh or 0.0, inputs.tariff_escalation, life)

    flows: list[float] = [-capex]
    for year in range(life):
        gen = generation[year]
        revenue = gen * offset_share * import_tariff[year]
        revenue += gen * export_share * export_tariff[year]
        net = revenue - inputs.opex_per_year
        # Calendar years are 1-based; `year` is the 0-based loop index.
        net -= float(inputs.year_costs.get(year + 1, 0.0))
        if year == 0:
            net += inputs.incentives_year_one
        flows.append(net)
    return flows


def total_capex(balance: EnergyBalance, inputs: FinanceInputs) -> float:
    """Capex from the per-kWp rate, the battery, and any lump sums.

    Benchmarks quoted per Wp (the UAE rows) must be multiplied by 1000 by the
    caller before they land here; this function does not guess at units.

    Battery cost is priced on *usable* kWh, matching how the benchmark rows are
    denominated. Omitting it would let a larger battery improve self-consumption,
    and therefore NPV, at no capital cost — which is precisely the decision the
    storage screen exists to inform.
    """
    if inputs.capex_per_kwp is None:
        raise FinanceError("capex_per_kwp is required")
    if balance.kwp <= 0.0:
        raise FinanceError("array capacity must be positive to cost a project")
    total = float(inputs.capex_per_kwp) * balance.kwp
    total += battery_capex(inputs)
    total += float(sum(inputs.capex_lump_sums.values()))
    return total


def battery_capex(inputs: FinanceInputs) -> float:
    """Capital cost of storage, priced per usable kWh."""
    if inputs.battery_usable_kwh <= 0.0:
        return 0.0
    if inputs.battery_capex_per_kwh is None:
        raise FinanceError(
            f"a {inputs.battery_usable_kwh:g} kWh battery is specified but no cost per kWh "
            "was supplied. A battery that improves self-consumption for free would make "
            "every storage comparison wrong."
        )
    return float(inputs.battery_capex_per_kwh) * float(inputs.battery_usable_kwh)


def evaluate(balance: EnergyBalance, inputs: FinanceInputs) -> FinanceResult:
    """Full appraisal from a year-one energy balance and the user's inputs."""
    flows = build_cashflows(balance, inputs)
    rate = float(inputs.discount_rate)
    life = int(inputs.project_life_years)
    capex = total_capex(balance, inputs)
    generation = degraded_generation(balance.generation_kwh, inputs.degradation_pct_per_year, life)
    # Mid-life replacements are lifetime costs and belong in the levelised figure
    # too, otherwise LCOE and the cashflow tell different stories about the same
    # project.
    opex = [inputs.opex_per_year + float(inputs.year_costs.get(y + 1, 0.0)) for y in range(life)]

    return FinanceResult(
        npv=npv(rate, flows),
        irr=irr(flows),
        simple_payback_years=payback_years(flows),
        discounted_payback_years=payback_years(discounted_cashflows(rate, flows)),
        lcoe_per_kwh=lcoe(capex, opex, generation, rate),
        total_capex=capex,
        cashflows=flows,
        discounted_cashflows=discounted_cashflows(rate, flows),
        currency=inputs.currency,
    )


# ---------------------------------------------------------------------------
# Carbon and the abatement curve
# ---------------------------------------------------------------------------


def abatement_tco2(
    annual_generation_kwh: list[float],
    grid_factor_t_per_mwh: float,
) -> float:
    """Lifetime avoided emissions at a fixed grid factor.

    A fixed factor is a simplification: every target market's grid is
    decarbonising, so this is an upper bound. Pass a declining factor series
    through `abatement_tco2_series` when one is available.
    """
    return sum(annual_generation_kwh) / 1000.0 * grid_factor_t_per_mwh


def abatement_tco2_series(
    annual_generation_kwh: list[float],
    grid_factor_t_per_mwh: list[float],
) -> float:
    """Lifetime avoided emissions against a year-by-year grid factor."""
    if len(annual_generation_kwh) != len(grid_factor_t_per_mwh):
        raise FinanceError("generation and grid factor series must be the same length")
    return sum(
        kwh / 1000.0 * factor
        for kwh, factor in zip(annual_generation_kwh, grid_factor_t_per_mwh)
    )


def macc_curve(steps: list[MaccStep]) -> list[dict[str, float | str]]:
    """Lay out marginal abatement steps as chart-ready bars.

    Steps arrive already additive — each one is the cost and abatement of adding
    it on top of everything to its left. Sorting by cost per tonne is the
    conventional presentation; the widths accumulate along the x axis.
    """
    priced = [s for s in steps if s.cost_per_tco2 is not None]
    ordered = sorted(priced, key=lambda s: s.cost_per_tco2)  # type: ignore[arg-type,return-value]
    bars: list[dict[str, float | str]] = []
    cursor = 0.0
    for step in ordered:
        width = step.delta_tco2
        bars.append(
            {
                "label": step.label,
                "x_start": cursor,
                "x_end": cursor + width,
                "width": width,
                "cost_per_tco2": step.cost_per_tco2,  # type: ignore[dict-item]
                "delta_capex": step.delta_capex,
                "delta_tco2": step.delta_tco2,
                "currency": step.currency,
            }
        )
        cursor += width
    return bars


@dataclass(frozen=True)
class Tranche:
    """One increment on the abatement ladder, before it is priced.

    `delta_annual_kwh` is the *additional* energy this step delivers in year one,
    over and above everything to its left.
    """

    label: str
    delta_capex: float
    delta_annual_kwh: float
    delta_opex_per_year: float = 0.0


def pv_tranches(total_capex: float, annual_kwh: float, count: int = 3,
                labels: list[str] | None = None) -> list[Tranche]:
    """Split the array into equal additive tranches.

    Equal splitting is a simplification: real capex per kWp falls with scale, so
    a true first tranche costs more per watt than the third. The benchmark rows
    carry that scale effect by system size if a caller wants to model it.
    """
    if count <= 0:
        raise FinanceError("need at least one PV tranche")
    names = labels or [f"PV tranche {i + 1}" for i in range(count)]
    return [
        Tranche(label=names[i], delta_capex=total_capex / count,
                delta_annual_kwh=annual_kwh / count)
        for i in range(count)
    ]


def battery_tranches(
    sizes_kwh: list[float],
    recovered_kwh: list[float],
    capex_per_kwh: float,
    opex_per_kwh_year: float = 0.0,
) -> list[Tranche]:
    """Additive battery increments.

    `recovered_kwh` must be energy that would otherwise have been *thrown away* —
    curtailed against an export limit or an export cap — not merely energy moved
    from export to self-consumption.

    This distinction decides whether the battery bars mean anything. Exported
    energy still displaces grid generation somewhere, so shifting a kWh from
    export to self-consumption changes who is paid for it, not how much carbon is
    avoided. Counting that shift as abatement would double-count energy already
    credited to the PV tranches. A battery abates carbon only where the
    alternative was curtailment — which is exactly the case a net-metering cap
    creates, and the Dubai row in the benchmark data says that cap is what
    "drives the battery case".
    """
    if len(sizes_kwh) != len(recovered_kwh):
        raise FinanceError("battery sizes and recovered energy must be the same length")
    tranches: list[Tranche] = []
    previous_size = 0.0
    for index, (size, recovered) in enumerate(zip(sizes_kwh, recovered_kwh)):
        delta_size = size - previous_size
        if delta_size <= 0.0:
            continue
        tranches.append(
            Tranche(
                label=f"Battery tranche {index + 1}",
                delta_capex=delta_size * capex_per_kwh,
                delta_annual_kwh=recovered,
                delta_opex_per_year=delta_size * opex_per_kwh_year,
            )
        )
        previous_size = size
    return tranches


def cleaning_tranche(
    annual_kwh: float,
    soiling_recovered_pct: float,
    annual_cleaning_cost: float,
    label: str = "Cleaning regime upgrade",
) -> Tranche:
    """A cleaning regime as an abatement step.

    `soiling_recovered_pct` has no defensible default — the soiling row in the
    benchmark data is tiered `gap` on purpose, because installer claims of
    15-25% loss are an upper bound rather than a design value. The caller must
    supply it, which is why it is a required argument with no fallback.
    """
    if soiling_recovered_pct < 0.0:
        raise FinanceError("soiling recovery cannot be negative")
    return Tranche(
        label=label,
        delta_capex=0.0,
        delta_annual_kwh=annual_kwh * soiling_recovered_pct / 100.0,
        delta_opex_per_year=annual_cleaning_cost,
    )


def macc_ladder(
    tranches: list[Tranche],
    grid_factor_t_per_mwh: float,
    degradation_pct_per_year: float,
    life_years: int,
    discount_rate: float | None = None,
    currency: str = "GBP",
) -> list[MaccStep]:
    """Price a ladder of additive tranches into MACC steps.

    Each step's abatement is its own additional energy over the project life,
    degraded annually. Where a discount rate is given, recurring opex is
    discounted into the step's cost so a step whose cost is all opex (a cleaning
    regime) is comparable with one whose cost is all capex (a PV tranche).
    """
    steps: list[MaccStep] = []
    for tranche in tranches:
        generation = degraded_generation(
            tranche.delta_annual_kwh, degradation_pct_per_year, life_years
        )
        if discount_rate is None:
            opex_cost = tranche.delta_opex_per_year * life_years
        else:
            opex_cost = sum(
                tranche.delta_opex_per_year / (1.0 + discount_rate) ** (y + 1)
                for y in range(life_years)
            )
        steps.append(
            MaccStep(
                label=tranche.label,
                delta_capex=tranche.delta_capex + opex_cost,
                delta_tco2=abatement_tco2(generation, grid_factor_t_per_mwh),
                delta_opex_per_year=tranche.delta_opex_per_year,
                currency=currency,
            )
        )
    return steps


def macc_step(
    label: str,
    delta_capex: float,
    delta_generation_kwh: list[float],
    grid_factor_t_per_mwh: float,
    delta_opex_per_year: float = 0.0,
    currency: str = "GBP",
) -> MaccStep:
    """Build one additive step from its incremental generation."""
    return MaccStep(
        label=label,
        delta_capex=delta_capex,
        delta_tco2=abatement_tco2(delta_generation_kwh, grid_factor_t_per_mwh),
        delta_opex_per_year=delta_opex_per_year,
        currency=currency,
    )

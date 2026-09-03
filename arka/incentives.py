"""Market incentive schemes, computed from the cited benchmark rows.

Incentives are not a single number per market — they are slab structures whose
kinks drive sizing decisions. India's PM Surya Ghar central financial assistance
is the clearest case: it pays per kWp up to 3 kWp and then stops dead, so a
5 kWp system receives exactly what a 3 kWp system receives. The CSV calls that
"the single biggest kink in Indian residential sizing economics", and a user
typing a number into a box will not reproduce it.

Every rate here is read from `data/cost_benchmarks.csv` through `benchmarks.py`.
Nothing is hardcoded, and a market with no cited scheme returns zero rather than
a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .benchmarks import BenchmarkNotFound, BenchmarkTable, GapError


@dataclass(frozen=True)
class Incentive:
    """What a scheme pays, and the arithmetic that got there."""

    amount: float
    currency: str
    scheme: str
    workings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    capped: bool = False

    def explain(self) -> str:
        return " ".join(self.workings) if self.workings else "No incentive applied."


NONE = Incentive(amount=0.0, currency="", scheme="none")


def pm_surya_ghar(kwp: float, table: BenchmarkTable) -> Incentive:
    """India's residential CFA. Slabs and cap come from the CSV.

    Structure: a per-kWp rate on the first 2 kW, a lower rate on the third kW,
    nothing above that, and a hard per-household cap. Residential grid-connected
    only — off-grid is explicitly excluded by the scheme.
    """
    if kwp <= 0.0:
        return NONE
    try:
        first_rate = table.get("PM Surya Ghar CFA first 2 kW", "India")
        third_rate = table.get("PM Surya Ghar CFA third kW", "India")
        cap = table.get("PM Surya Ghar CFA cap per household", "India")
    except (BenchmarkNotFound, GapError):
        return NONE

    def gross_at(size: float) -> float:
        """Slab total before the household cap."""
        first = min(size, 2.0)
        third = min(max(size - 2.0, 0.0), 1.0)
        return first * first_rate + third * third_rate

    first_slab = min(kwp, 2.0)
    third_slab = min(max(kwp - 2.0, 0.0), 1.0)
    gross = gross_at(kwp)
    amount = min(gross, cap)

    # "Capped" means adding capacity earns nothing further. That can happen two
    # ways — the household cap binds, or the slabs simply stop — and both matter
    # equally to a sizing decision, so the flag is derived by asking what a
    # slightly larger array would receive rather than by hardcoding the kink.
    capped = min(gross_at(kwp + 0.5), cap) <= amount + 1e-9

    workings = [
        f"{first_slab:.2f} kW at {first_rate:,.0f}/kW = {first_slab * first_rate:,.0f}.",
    ]
    if third_slab > 0.0:
        workings.append(
            f"{third_slab:.2f} kW at {third_rate:,.0f}/kW = {third_slab * third_rate:,.0f}."
        )
    if kwp > 3.0:
        workings.append(f"Nothing above 3 kW: the {kwp:.1f} kWp array is paid as a 3 kWp one.")
    if gross > cap:
        workings.append(f"Capped at {cap:,.0f} per household.")
    elif capped and kwp <= 3.0:
        workings.append(f"At the {cap:,.0f} household maximum.")

    return Incentive(
        amount=amount,
        currency="INR",
        scheme="PM Surya Ghar (residential CFA)",
        workings=workings,
        sources=[table.find("PM Surya Ghar CFA first 2 kW", "India").citation()],
        capped=capped,
    )


def uk_annual_investment_allowance(total_capex: float, table: BenchmarkTable,
                                   band: str = "central") -> Incentive:
    """UK first-year tax relief, as a percentage of capex.

    The effective value depends on the company's corporation tax rate, so the
    CSV carries a band rather than a single figure. Modelled as a year-one cash
    inflow, which is what the source row says to do.
    """
    if total_capex <= 0.0:
        return NONE
    try:
        row = table.find("Annual Investment Allowance first-year relief", "UK")
        pct = row.value(band)
    except (BenchmarkNotFound, GapError):
        return NONE
    amount = total_capex * pct / 100.0
    return Incentive(
        amount=amount,
        currency="GBP",
        scheme="Annual Investment Allowance",
        workings=[f"{pct:g}% of {total_capex:,.0f} capex = {amount:,.0f}, taken in year 1."],
        sources=[row.citation()],
    )


def for_market(market: str, kwp: float, total_capex: float, table: BenchmarkTable,
               band: str = "central") -> Incentive:
    """The applicable scheme for a market, or `NONE` where none is cited.

    UAE has no capital subsidy in the source data — its incentive is the
    net-metering scheme, which shows up in the energy balance rather than as a
    cash inflow, so returning zero here is correct rather than a gap.
    """
    key = market.strip().lower()
    if key == "india":
        return pm_surya_ghar(kwp, table)
    if key == "uk":
        return uk_annual_investment_allowance(total_capex, table, band)
    return NONE

# Arka — PV siting and sizing planner

Draw a solar array on a satellite map and get generation, storage sizing and
financial analysis from open data. Rooftop, ground-mount and floating.

Markets: United Kingdom, India, United Arab Emirates.

## Status

| Capability | State |
|---|---|
| Rooftop siting and packing | built, tested |
| Ground-mount | interface only — raises `NotImplementedError` |
| Floating | interface only — raises `NotImplementedError` |
| PVGIS yield | built; parsing and caching tested against fixtures, live endpoint not yet exercised |
| Battery dispatch and sizing | built, tested |
| Financial model (NPV, IRR, payback, LCOE) | built, tested |
| Market incentives (PM Surya Ghar, UK AIA) | built, tested |
| UAE: net metering, per-Wp costs, DEWA rows | built, tested |
| Marginal abatement cost curve | all six tranches built, tested |
| Agentic intake and orchestration | built, verified live against `gemini-flash-latest` |
| One-page HTML report with satellite image | built |
| Clear-sky physical guard on PVGIS output | built, tested |

## Design principle

The language model never produces a number. It parses input, orchestrates
deterministic functions, retrieves current regulations and writes the narrative.
Every energy, currency and carbon figure comes from `pvlib`, the dispatch model,
or the cited benchmark data in `data/cost_benchmarks.csv`.

This is enforced in code, not only in the prompt. `arka/agent/client.py` screens
model prose with `verify_no_invented_numbers`: any numeral that cannot be traced
back to a computed figure (allowing for rounding and unit rescaling) raises
`NumberGuardError` rather than reaching the report.

## Data provenance

Cost and carbon benchmarks are cited and tiered by source quality in
`data/cost_benchmarks.csv`. Rows tiered `gap` are deliberately empty — there is
no defensible open source for them and they must be supplied by the user.
`benchmarks.py` raises `GapError` rather than returning a plausible-looking
number, and the Finance screen refuses to compute until every gap is filled.

Irradiance data: PVGIS 5.3 (PVGIS-SARAH3), European Commission JRC.

## Running

```bash
pip install -r requirements.txt
streamlit run app.py
```

The agent layer is optional. Set `GEMINI_API_KEY` in the environment or in
`.streamlit/secrets.toml` to enable it; every screen works without it.

## Layout

```
app.py                  Streamlit shell, six screens, routing only
arka/
  scenario.py           Scenario dataclass, ArrayType enum, serialisation
  geometry.py           polygon area, azimuth, exclusions, pack()
  modules.py            pvlib CEC module lookup and footprint derivation
  resource.py           PVGIS seriescalc client, retries, on-disk cache
  dispatch.py           hourly battery dispatch, self-consumption, sweeps
  finance.py            NPV, IRR, payback, LCOE, abatement, MACC layout
  benchmarks.py         loads data/cost_benchmarks.csv, market lookup, gap rule
  charts.py             plotly figure builders, rendering only
  agent/                schemas, deterministic tool registry, genai wrapper
data/cost_benchmarks.csv
tests/
```

## Tests

```bash
pytest
```

`geometry`, `dispatch` and `finance` carry the numbers that matter and are
tested hardest; `benchmarks` is tested for the gap rule, `incentives` for the
subsidy slabs and their kinks, and `agent` for the number guard. Nothing in the
suite touches the network.

## The clear-sky guard

PVGIS is the only irradiance source, and its response shape is the one thing
this project cannot verify from a sandbox. So `clearsky.py` computes, entirely
offline with pvlib, what the array would produce under a full year of clear
skies — the brightest sky a site can have, with cells held at 25 °C and no
losses applied. That is a hard physical ceiling: a correct PVGIS series sits
well below it (about half in the UK, four-fifths in the Gulf), and one that has
been scaled by a thousand, doubled, time-shifted or truncated lands above it or
implausibly far beneath it and is refused on the Yield screen. The ceiling is a
validator, never a displayed yield.

## What the numbers rest on

Two modelling choices are worth knowing before you quote anything.

**A battery abates carbon only where energy would otherwise be curtailed.**
Exported energy displaces grid generation either way, so shifting a kWh from
export to self-consumption changes who is paid for it, not how much carbon is
avoided. Counting that shift would double-count abatement already credited to
the PV tranches. The battery bars on the MACC therefore use curtailment
recovered against an export limit — which is exactly the case a net-metering cap
creates, and the Dubai row in the benchmark data says that cap is what "drives
the battery case".

**Export under net metering is only worth the retail rate while there is an
import left to cancel.** Under DEWA's Shams Dubai scheme export is credited
against consumption rather than paid out, so past the point where the site's own
annual consumption is cancelled, further generation earns only the surplus rate —
nothing, in DEWA's case. That ceiling is what caps the value of oversizing and,
as the benchmark data puts it, "drives the battery case". Set
`FinanceInputs.net_metering` for any market that credits rather than pays.

**Dispatch reserves capacity for clipped energy.** A controller that charges
from any available surplus fills the pack in the morning and has no headroom
left when clipping happens around midday — in testing it was full for 372 of the
hours when clipping occurred, making a small battery look useless and producing
*increasing* marginal returns. `BatterySpec.curtailment_reserve_fraction` holds
part of the pack back for energy that would otherwise be thrown away, which is
what a real controller with an export limit does.

## Known gaps

- No live PVGIS call has been made. The client, cache and parser are unit tested
  against fixture payloads; the first real call will confirm the response shape.
  The clear-sky guard now catches a mis-parsed response at the point of use,
  but it cannot confirm that a plausible-looking series is the *right* one.
- Module dimensions are derived from CEC cell area and the cell grid, because the
  CEC file carries no frame size. Mean error against published datasheets is
  about 2%, but back-contact formats are worse. Override from the datasheet where
  it matters — row pitch, and therefore capacity, depends on it.
- Load shapes are synthetic. Replace with metered half-hourly data before any
  figure leaves the building.
- Shading from surrounding objects is not modelled. Only row-to-row self-shading
  sets the row pitch.
- PV tranches on the MACC split capex evenly, so they ignore the real fall in
  cost per kWp with scale. The benchmark rows carry that scale effect by system
  size if it matters to you.
- Marginal curtailment recovery is not guaranteed monotonic across battery
  sizes: the reserve fraction interacts with pack size. The curve sorts by cost
  per tonne regardless.
- Dispatch has no foresight. A perfect-foresight or MPC controller would beat the
  reserve heuristic.
- The UAE has no published present-day grid emission factor. The CSV carries a
  2019 baseline and a 2030 target; neither is a current figure, and the app says
  so rather than letting either stand in. Supply one from the DEWA Sustainability
  Report for Dubai, or ADDC/EWEC for Abu Dhabi.
- Net metering holds the self-consumed, credited and paid-export shares fixed as
  output degrades. The split shifts slightly year to year, but not enough to
  justify re-running dispatch for all 25 years.

## Licence

MIT

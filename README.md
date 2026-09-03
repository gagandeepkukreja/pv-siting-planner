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
| Marginal abatement cost curve | PV tranches built; battery and cleaning tranches not yet |
| Agentic intake and orchestration | scaffolded — schemas, tool registry and the number guard are in place and tested; no live model call has been made |

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

185 tests. `geometry`, `dispatch` and `finance` carry the numbers that matter and
are tested hardest; `benchmarks` is tested for the gap rule and `agent` for the
number guard. Nothing in the suite touches the network.

## Known gaps

- No live PVGIS call has been made yet. The client, cache and parser are unit
  tested against fixture payloads; the first real call will confirm the response
  shape.
- Module dimensions are derived from CEC cell area and a cell-count-to-width
  table, because the CEC file carries no frame size. Override from the datasheet
  where it matters — row pitch, and therefore capacity, depends on it.
- Load shapes are synthetic. Replace with metered half-hourly data before any
  figure leaves the building.
- MACC covers PV tranches only. Battery and cleaning-regime tranches are next.
- Shading from surrounding objects is not modelled. Only row-to-row
  self-shading sets the row pitch.

## Licence

MIT

# Arka — PV siting and sizing planner

Draw a solar array on a satellite map, get generation, storage sizing and
financial analysis from open data. Target markets: UK, India, UAE.

## Non-negotiable rules

1. **The LLM never produces a number.** Gemini is used for input parsing, tool
   orchestration, regulation retrieval and narrative. Every kWh, currency and
   tonne figure comes from `pvlib`, the dispatch model or `data/cost_benchmarks.csv`.
   If a code path would let model output reach a displayed figure, that path is wrong.
2. **No cost or emission figure is hardcoded.** All of it lives in
   `data/cost_benchmarks.csv` and is read at runtime. Adding a constant to a
   `.py` file is a bug.
3. **Source tiers are carried through to the UI.** The CSV has a `source_tier`
   column (`authoritative`, `market-survey`, `installer-marketing`, `gap`,
   `convention`). The assumptions table in the report shows the tier next to
   every value. Rows tiered `gap` are empty on purpose — they must be user
   inputs, never silently defaulted.
4. **Assumptions are always visible.** Every output screen can show what fed it.
5. Two floats never compared for equality; every displayed number rounded at
   the presentation layer, not in the model.

## Scope

Three array types. Only `ROOFTOP` is implemented initially, but the interface
is built for all three from day one — `GROUND_MOUNT` and `FLOATING` raise
`NotImplementedError` rather than being absent.

- `ROOFTOP` — irregular polygon, exclusion zones punched out, flush or ballasted
- `GROUND_MOUNT` — row layout driven by ground coverage ratio and row-to-row
  shading; fixed tilt first, single-axis tracking later
- `FLOATING` — coverage ratio limits, freeboard, anchoring corridors

## Architecture

Single `Scenario` dataclass is the app state. Every screen reads and writes it.
Nothing else holds state.

```
app.py                  Streamlit shell, six screens, routing only
arka/
  scenario.py           Scenario dataclass, ArrayType enum, serialisation
  geometry.py           polygon area, azimuth from longest edge, exclusion
                        differencing, pack(polygon, array_type, params)
  modules.py            pvlib CEC module lookup, filtering by technology
  resource.py           PVGIS seriescalc client, retries, on-disk cache
  dispatch.py           hourly battery dispatch, self-consumption
  finance.py            NPV, IRR, simple + discounted payback, LCOE, MACC
  benchmarks.py         loads data/cost_benchmarks.csv, market lookup
  charts.py             plotly figure builders; rendering only, no arithmetic
  incentives.py         PM Surya Ghar slabs, UK AIA, computed from the CSV
  basemap.py            satellite tiles for the report export, fetched server-side
  clearsky.py           offline physical ceiling on yield via pvlib; validates PVGIS
  agent/
    schema.py           Pydantic schemas for structured intake
    tools.py            deterministic functions exposed as tool declarations
    client.py           google-genai wrapper
data/
  cost_benchmarks.csv   costing and carbon benchmarks, cited, tiered
tests/
```

### Screens

1. **Site** — map, array type picker, polygon drawing, exclusion zones
2. **Array** — module picker, tilt/azimuth/pitch, live packing preview
3. **Yield** — monthly generation, 24x365 hourly heatmap, specific yield
4. **Storage** — load profile overlay, battery sweep, self-consumption curve
5. **Finance** — cashflow waterfall, cumulative discounted cashflow, MACC
6. **Report** — one-page HTML export with satellite image, layout, assumptions

## Technical constraints

- **PVGIS blocks browser AJAX.** All PVGIS calls go through Python. Never
  attempt a client-side fetch.
- PVGIS 5.3 `seriescalc` endpoint, `PVGIS-SARAH3` database — covers UK, India
  and the Gulf. Hourly (8760) output is required; the battery model depends on it.
- Cache every PVGIS response to disk keyed on lat/lon/tilt/azimuth. The API is
  slow and rate-limited.
- `google-genai` SDK (`from google import genai`), Interactions API, model alias
  `gemini-flash-latest`. Not the deprecated `google-generativeai`.
- Streamlit + `streamlit-folium`, Esri World Imagery tiles with an OSM toggle.

## MACC

Bars must be **marginal and additive**, not a set of mutually exclusive
scenarios placed side by side. Each increment is the cost and abatement of
adding that step on top of everything to its left: PV tranche 1, PV tranche 2,
PV tranche 3, battery tranche 1, battery tranche 2, cleaning regime upgrade.
Every scenario object carries `delta_capex` and `delta_tco2` so the chart is a
rendering job.

## Build order

1. Polygon capture → area → azimuth
2. Packing → module count → kWp
3. PVGIS client → annual and monthly yield
4. Load profile → dispatch → battery sweep
5. Finance reading the CSV
6. Charts
7. Gemini intake and orchestration
8. UI styling last

Get one real yield number on screen before any styling work.

## Style

- Type hints throughout, dataclasses over dicts for structured state
- Pure functions in the core library; Streamlit only in `app.py`
- Every function that consumes a benchmark takes it as an argument — no module
  reaching into the CSV loader directly except `benchmarks.py`
- Tests for `geometry.py`, `dispatch.py` and `finance.py`; those three carry the
  numbers that matter

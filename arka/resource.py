"""PVGIS 5.3 `seriescalc` client.

PVGIS blocks browser AJAX, so every call goes through Python — never attempt a
client-side fetch (CLAUDE.md technical constraints).

Responses are cached on disk keyed on lat/lon/tilt/azimuth/loss/database. The
API is slow and rate-limited, and the battery model needs the full 8760, so a
cache miss is expensive and a cache hit is the normal case.

Output power scales linearly with `peakpower`, so every request is made at
1 kWp and scaled afterwards. That keeps one cache entry valid across every
array size at a given site and orientation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

import requests

from .scenario import YieldResult

log = logging.getLogger(__name__)

PVGIS_ENDPOINT = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
DEFAULT_DATABASE = "PVGIS-SARAH3"   # covers UK, India and the Gulf
DEFAULT_LOSS_PCT = 14.0             # PVGIS default system loss
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "pvgis"

HOURS_PER_YEAR = 8760
_MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class PVGISError(RuntimeError):
    """PVGIS could not be reached, or returned something unusable."""


@dataclass(frozen=True)
class PVGISRequest:
    """Everything that identifies one cached PVGIS response."""

    lat: float
    lon: float
    tilt_deg: float
    azimuth_deg: float
    loss_pct: float = DEFAULT_LOSS_PCT
    database: str = DEFAULT_DATABASE
    mounting: str = "building"   # 'building' for rooftop, 'free' for ground-mount

    def cache_key(self) -> str:
        payload = "|".join(
            (
                f"{self.lat:.5f}",
                f"{self.lon:.5f}",
                f"{self.tilt_deg:.2f}",
                f"{self.azimuth_deg:.2f}",
                f"{self.loss_pct:.2f}",
                self.database,
                self.mounting,
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def params(self) -> dict[str, object]:
        return {
            "lat": round(self.lat, 5),
            "lon": round(self.lon, 5),
            "raddatabase": self.database,
            "outputformat": "json",
            "pvcalculation": 1,
            "peakpower": 1.0,
            "loss": self.loss_pct,
            "angle": round(self.tilt_deg, 2),
            "aspect": round(to_pvgis_aspect(self.azimuth_deg), 2),
            "mountingplace": self.mounting,
            "components": 0,
        }


def to_pvgis_aspect(azimuth_deg: float) -> float:
    """Convert a north-referenced azimuth (pvlib) to PVGIS's south-referenced aspect.

    pvlib: 0 = north, 90 = east, 180 = south, 270 = west.
    PVGIS: 0 = south, -90 = east, 90 = west.
    """
    return (azimuth_deg - 180.0 + 180.0) % 360.0 - 180.0


def from_pvgis_aspect(aspect_deg: float) -> float:
    """Inverse of `to_pvgis_aspect`."""
    return (aspect_deg + 180.0) % 360.0


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_raw(
    request: PVGISRequest,
    cache_dir: Path | str = CACHE_DIR,
    timeout_s: float = 60.0,
    retries: int = 4,
    session: requests.Session | None = None,
    refresh: bool = False,
) -> dict:
    """The raw PVGIS JSON for a request, from disk cache when available."""
    cache_dir = Path(cache_dir)
    cache_file = cache_dir / f"{request.cache_key()}.json"
    if cache_file.exists() and not refresh:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("discarding unreadable PVGIS cache entry %s", cache_file)

    http = session or requests.Session()
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = http.get(PVGIS_ENDPOINT, params=request.params(), timeout=timeout_s)
            if response.status_code == 400:
                raise PVGISError(
                    f"PVGIS rejected the request: {response.text.strip()[:300]}. "
                    f"Is {request.lat:.4f},{request.lon:.4f} inside {request.database} coverage?"
                )
            response.raise_for_status()
            payload = response.json()
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            return payload
        except PVGISError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            backoff = 2.0 ** attempt
            log.warning("PVGIS attempt %d failed (%s), retrying in %.0fs", attempt + 1, exc, backoff)
            time.sleep(backoff)

    raise PVGISError(
        f"PVGIS unreachable after {retries} attempts: {last_error}. "
        "There is no offline fallback by design — a fabricated yield is worse than no yield."
    ) from last_error


def hourly_yield(
    lat: float,
    lon: float,
    tilt_deg: float,
    azimuth_deg: float,
    kwp: float,
    loss_pct: float = DEFAULT_LOSS_PCT,
    database: str = DEFAULT_DATABASE,
    mounting: str = "building",
    cache_dir: Path | str = CACHE_DIR,
    refresh: bool = False,
    session: requests.Session | None = None,
) -> YieldResult:
    """8760 hourly kWh for an array, plus monthly and annual totals."""
    if kwp <= 0.0:
        raise ValueError("kwp must be positive; pack the array before asking for yield")
    request = PVGISRequest(
        lat=lat,
        lon=lon,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        loss_pct=loss_pct,
        database=database,
        mounting=mounting,
    )
    payload = fetch_raw(request, cache_dir=cache_dir, refresh=refresh, session=session)
    return parse_hourly(payload, kwp=kwp, loss_pct=loss_pct, database=database)


def parse_hourly(payload: dict, kwp: float, loss_pct: float = DEFAULT_LOSS_PCT,
                 database: str = DEFAULT_DATABASE) -> YieldResult:
    """Turn a PVGIS response into a single representative 8760 at `kwp`.

    PVGIS returns every year in the database period. Averaging them would flatten
    the peaks the battery model depends on, so we keep one real year: the one
    whose annual total is closest to the multi-year mean. That preserves genuine
    hour-to-hour variability while staying representative.
    """
    try:
        records = payload["outputs"]["hourly"]
    except (KeyError, TypeError) as exc:
        raise PVGISError(f"unexpected PVGIS payload shape: {list(payload)[:5]}") from exc
    if not records:
        raise PVGISError("PVGIS returned an empty hourly series")

    by_year: dict[int, list[tuple[str, float]]] = {}
    for record in records:
        stamp = str(record.get("time", ""))
        if len(stamp) < 11:
            continue
        year = int(stamp[0:4])
        # Skip 29 February so every year is exactly 8760 hours.
        if stamp[4:8] == "0229":
            continue
        watts = float(record.get("P", 0.0) or 0.0)
        by_year.setdefault(year, []).append((stamp, watts))

    complete = {y: rows for y, rows in by_year.items() if len(rows) == HOURS_PER_YEAR}
    if not complete:
        raise PVGISError(
            "PVGIS returned no complete year of hourly data "
            f"(years seen: {sorted(by_year)}, longest {max((len(v) for v in by_year.values()), default=0)} hours)"
        )

    totals = {y: sum(w for _, w in rows) for y, rows in complete.items()}
    target = mean(totals.values())
    chosen = min(totals, key=lambda y: abs(totals[y] - target))
    rows = sorted(complete[chosen])

    # PVGIS P is watts for a 1 kWp system; one hourly step is one hour, so
    # W -> kWh is a division by 1000, then a linear scale to the real array.
    hourly = [watts / 1000.0 * kwp for _, watts in rows]
    monthly = monthly_totals(hourly)
    return YieldResult(
        annual_kwh=float(sum(hourly)),
        monthly_kwh=monthly,
        hourly_kwh=hourly,
        kwp=kwp,
        database=database,
        year_range=(min(complete), max(complete)),
        system_loss_pct=loss_pct,
        source=f"PVGIS 5.3 ({database}), representative year {chosen}",
    )


def monthly_totals(hourly: list[float]) -> list[float]:
    """Sum an 8760 series into twelve calendar months (non-leap year)."""
    if len(hourly) != HOURS_PER_YEAR:
        raise ValueError(f"expected {HOURS_PER_YEAR} hourly values, got {len(hourly)}")
    out: list[float] = []
    cursor = 0
    for days in _MONTH_LENGTHS:
        hours = days * 24
        out.append(float(sum(hourly[cursor:cursor + hours])))
        cursor += hours
    return out


def hourly_to_heatmap(hourly: list[float]) -> list[list[float]]:
    """Reshape an 8760 into 24 rows (hour of day) by 365 columns (day of year)."""
    if len(hourly) != HOURS_PER_YEAR:
        raise ValueError(f"expected {HOURS_PER_YEAR} hourly values, got {len(hourly)}")
    return [[hourly[day * 24 + hour] for day in range(365)] for hour in range(24)]


def sanity_check_specific_yield(result: YieldResult, benchmark_low: float,
                                benchmark_high: float) -> str | None:
    """Compare specific yield against a market benchmark band.

    The benchmark is passed in — this module never reads the CSV itself.
    """
    specific = result.specific_yield_kwh_per_kwp
    if not math.isfinite(specific) or specific <= 0.0:
        return "specific yield is zero; check the array capacity"
    if specific < benchmark_low:
        return (
            f"specific yield {specific:.0f} kWh/kWp is below the benchmark band "
            f"({benchmark_low:.0f}–{benchmark_high:.0f}); check tilt, azimuth and shading"
        )
    if specific > benchmark_high:
        return (
            f"specific yield {specific:.0f} kWh/kWp is above the benchmark band "
            f"({benchmark_low:.0f}–{benchmark_high:.0f}); check the system loss assumption"
        )
    return None

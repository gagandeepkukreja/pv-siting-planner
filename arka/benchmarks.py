"""Loader for `data/cost_benchmarks.csv`.

This is the only module allowed to read the CSV (CLAUDE.md style rule). Every
other function takes the benchmark it needs as an argument.

Two rules are enforced here rather than left to callers:

* No cost or emission figure is hardcoded anywhere else in the codebase.
* Rows tiered `gap` are empty on purpose. Asking for one without supplying an
  override raises `GapError` rather than returning a plausible-looking number.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "data" / "cost_benchmarks.csv"

# Market key used for rows that apply to every market.
ALL_MARKETS = "all"


class Tier(str, Enum):
    """Source quality, carried through to the assumptions table in the UI."""

    AUTHORITATIVE = "authoritative"
    MARKET_SURVEY = "market-survey"
    INSTALLER_MARKETING = "installer-marketing"
    CONVENTION = "convention"
    GAP = "gap"

    @property
    def rank(self) -> int:
        order = {
            Tier.AUTHORITATIVE: 0,
            Tier.MARKET_SURVEY: 1,
            Tier.INSTALLER_MARKETING: 2,
            Tier.CONVENTION: 3,
            Tier.GAP: 4,
        }
        return order[self]

    @property
    def caveat(self) -> str:
        return {
            Tier.AUTHORITATIVE: "Government, regulator or standards body.",
            Tier.MARKET_SURVEY: "Derived from surveyed market pricing.",
            Tier.INSTALLER_MARKETING: "Installer-published. Indicative, not audited.",
            Tier.CONVENTION: "Modelling convention, not a citation.",
            Tier.GAP: "No defensible open source. Must be supplied by the user.",
        }[self]


class Band(str, Enum):
    LOW = "low"
    CENTRAL = "central"
    HIGH = "high"


class GapError(LookupError):
    """A `gap`-tiered row was requested without a user-supplied override."""


class BenchmarkNotFound(LookupError):
    """No row matched the requested market and parameter."""


@dataclass(frozen=True)
class Benchmark:
    """One row of the CSV, parsed. Empty numeric cells become None."""

    market: str
    category: str
    parameter: str
    low: float | None
    central: float | None
    high: float | None
    unit: str
    currency: str
    as_of: str
    source: str
    source_url: str
    source_tier: Tier
    remarks: str

    @property
    def is_gap(self) -> bool:
        return self.source_tier is Tier.GAP or self.central is None

    def value(self, band: Band | str = Band.CENTRAL) -> float:
        """Return one band of the row, or raise if the row is a gap."""
        band = Band(band)
        if self.is_gap:
            raise GapError(
                f"{self.market}/{self.parameter} is tiered '{self.source_tier.value}' and has "
                f"no value. Supply it as a user input. Guidance: {self.remarks}"
            )
        got = getattr(self, band.value)
        if got is None:
            raise GapError(f"{self.market}/{self.parameter} has no '{band.value}' band")
        return float(got)

    def citation(self) -> str:
        bits = [self.source or "unsourced"]
        if self.as_of:
            bits.append(f"({self.as_of})")
        return " ".join(bits)


class BenchmarkTable:
    """An immutable view over benchmark rows, filterable by market."""

    def __init__(self, rows: list[Benchmark]) -> None:
        self._rows = list(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    @property
    def rows(self) -> list[Benchmark]:
        return list(self._rows)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CSV) -> "BenchmarkTable":
        return cls(_read_rows(Path(path)))

    def for_market(self, market: str, include_all: bool = True) -> "BenchmarkTable":
        """Rows for one market, plus the cross-market `all` rows by default."""
        key = _normalise(market)
        keep = {key} | ({ALL_MARKETS} if include_all else set())
        return BenchmarkTable([r for r in self._rows if _normalise(r.market) in keep])

    def category(self, category: str) -> "BenchmarkTable":
        key = _normalise(category)
        return BenchmarkTable([r for r in self._rows if _normalise(r.category) == key])

    def find(self, parameter: str, market: str | None = None) -> Benchmark:
        """Exact-then-substring lookup on the parameter name."""
        pool = self.for_market(market)._rows if market else self._rows
        key = _normalise(parameter)
        exact = [r for r in pool if _normalise(r.parameter) == key]
        if exact:
            return _best(exact)
        partial = [r for r in pool if key in _normalise(r.parameter)]
        if partial:
            return _best(partial)
        raise BenchmarkNotFound(
            f"no benchmark matching {parameter!r}"
            + (f" for market {market!r}" if market else "")
        )

    def get(
        self,
        parameter: str,
        market: str | None = None,
        band: Band | str = Band.CENTRAL,
        override: float | None = None,
    ) -> float:
        """Value for a parameter. `override` wins, which is how gaps get filled."""
        if override is not None:
            return float(override)
        return self.find(parameter, market).value(band)

    def gaps(self) -> list[Benchmark]:
        """Rows the user must fill in before a result can be trusted."""
        return [r for r in self._rows if r.is_gap]

    def currency_for(self, market: str) -> str:
        """The currency the market's rows are denominated in."""
        for row in self.for_market(market, include_all=False)._rows:
            if row.currency and row.currency != "-":
                return row.currency
        raise BenchmarkNotFound(f"no currency found for market {market!r}")

    def assumptions(self, market: str | None = None) -> list[dict[str, str]]:
        """Flat rows for the assumptions table. Tier sits next to every value."""
        pool = self.for_market(market)._rows if market else self._rows
        out = []
        for r in sorted(pool, key=lambda r: (r.source_tier.rank, r.category, r.parameter)):
            out.append(
                {
                    "parameter": r.parameter,
                    "market": r.market,
                    "category": r.category,
                    "value": "—" if r.is_gap else _format_band(r),
                    "unit": r.unit,
                    "currency": r.currency,
                    "tier": r.source_tier.value,
                    "source": r.citation(),
                    "url": r.source_url,
                    "remarks": r.remarks,
                }
            )
        return out


@lru_cache(maxsize=8)
def load(path: str | Path = DEFAULT_CSV) -> BenchmarkTable:
    """Cached loader. Call this rather than constructing the table yourself."""
    return BenchmarkTable.load(path)


def _read_rows(path: Path) -> list[Benchmark]:
    if not path.exists():
        raise FileNotFoundError(f"benchmark CSV not found at {path}")
    rows: list[Benchmark] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            rows.append(
                Benchmark(
                    market=(raw.get("market") or "").strip(),
                    category=(raw.get("category") or "").strip(),
                    parameter=(raw.get("parameter") or "").strip(),
                    low=_number(raw.get("low")),
                    central=_number(raw.get("central")),
                    high=_number(raw.get("high")),
                    unit=(raw.get("unit") or "").strip(),
                    currency=(raw.get("currency") or "").strip(),
                    as_of=(raw.get("as_of") or "").strip(),
                    source=(raw.get("source") or "").strip(),
                    source_url=(raw.get("source_url") or "").strip(),
                    source_tier=_tier(raw.get("source_tier")),
                    remarks=(raw.get("remarks") or "").strip(),
                )
            )
    return rows


def _number(cell: str | None) -> float | None:
    if cell is None:
        return None
    text = cell.strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _tier(cell: str | None) -> Tier:
    text = (cell or "").strip().lower()
    try:
        return Tier(text)
    except ValueError:
        return Tier.GAP


def _normalise(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _best(candidates: list[Benchmark]) -> Benchmark:
    """Prefer the most authoritative row, then a market-specific one over `all`."""
    return min(
        candidates,
        key=lambda r: (r.source_tier.rank, _normalise(r.market) == ALL_MARKETS),
    )


def _format_band(row: Benchmark) -> str:
    if row.low is None or row.high is None:
        return f"{row.central:g}"
    if row.low == row.high:
        return f"{row.central:g}"
    return f"{row.low:g} – {row.central:g} – {row.high:g}"

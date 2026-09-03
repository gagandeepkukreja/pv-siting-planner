"""Pydantic schemas for structured intake.

These are the shapes the model is allowed to emit. Note what is absent: no
field here is an energy, currency or carbon figure. The model may state that a
roof is flat, that the market is India, that the user wants a battery — it may
not state how many kWh anything produces.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..scenario import ArrayType, Market, Mounting, Orientation


class SiteIntake(BaseModel):
    """What the model extracted about the site from free text."""

    site_name: str | None = Field(None, description="Name the user gave the site")
    market: Market | None = Field(None, description="UK, India or UAE")
    address_or_place: str | None = Field(
        None, description="Free-text location to geocode; never a made-up coordinate"
    )
    array_type: ArrayType = Field(
        ArrayType.ROOFTOP, description="Rooftop, ground-mount or floating"
    )
    mounting: Mounting | None = Field(
        None, description="Flush to a pitched roof, or ballasted on a flat one"
    )
    roof_description: str | None = Field(
        None, description="Anything said about roof condition, age, obstructions"
    )
    obstructions: list[str] = Field(
        default_factory=list, description="Plant, rooflights, walkways to exclude"
    )
    wants_battery: bool | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    unresolved: list[str] = Field(
        default_factory=list,
        description="Things the user must confirm before any figure is produced",
    )


class ArrayIntake(BaseModel):
    """Array preferences the model picked up. Dimensions come from the CEC database."""

    module_query: str | None = Field(
        None, description="Free-text module search, e.g. 'LONGi 550 bifacial'"
    )
    technology: str | None = None
    orientation: Orientation | None = None
    tilt_deg: float | None = Field(None, ge=0.0, le=90.0)
    azimuth_deg: float | None = Field(None, ge=0.0, lt=360.0)
    edge_setback_m: float | None = Field(None, ge=0.0, le=10.0)

    @field_validator("tilt_deg", "azimuth_deg", "edge_setback_m")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and v != v:  # NaN
            raise ValueError("must be a finite number")
        return v


class FinanceIntake(BaseModel):
    """User-supplied financial inputs — the `gap`-tiered rows.

    These are the user's numbers repeated back, not the model's numbers. The
    UI shows them for confirmation before anything is computed.
    """

    discount_rate_pct: float | None = Field(None, ge=0.0, le=50.0)
    tariff_escalation_pct: float | None = Field(None, ge=-20.0, le=50.0)
    import_tariff_per_kwh: float | None = Field(None, ge=0.0)
    export_tariff_per_kwh: float | None = Field(None, ge=0.0)
    annual_load_kwh: float | None = Field(None, ge=0.0)
    project_life_years: int | None = Field(None, ge=1, le=40)
    stated_by_user: bool = Field(
        True, description="False if inferred rather than stated; the UI flags inferred values"
    )


class Intake(BaseModel):
    """The whole structured intake for one conversational turn."""

    site: SiteIntake = Field(default_factory=SiteIntake)
    array: ArrayIntake = Field(default_factory=ArrayIntake)
    finance: FinanceIntake = Field(default_factory=FinanceIntake)
    next_question: str | None = Field(
        None, description="The single most useful thing to ask next, or null if ready"
    )


class Narrative(BaseModel):
    """Prose for the report. Figures are quoted from the computed results only."""

    headline: str
    body: str
    caveats: list[str] = Field(default_factory=list)
    figures_used: list[str] = Field(
        default_factory=list,
        description="Every numeral in the body, verbatim, so it can be checked against the model outputs",
    )

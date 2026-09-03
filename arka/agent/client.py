"""google-genai wrapper, plus the guard that keeps model output away from figures.

Uses the `google-genai` SDK (`from google import genai`), not the deprecated
`google-generativeai`. Model alias: `gemini-flash-latest`.

The design principle is enforced here, not merely requested in a prompt. Two
mechanisms:

* Tool calling is one-directional. The model picks a function and arguments;
  `tools.call` computes the result. A model-supplied result is never accepted.
* Narrative text is screened by `verify_no_invented_numbers` before it is
  displayed. Any numeral that does not trace back to a computed figure is
  reported, and the caller decides whether to re-prompt or refuse to render.

The SDK is imported lazily so the rest of the app — and the test suite — works
without an API key.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import tools
from .schema import Intake, Narrative

DEFAULT_MODEL = "gemini-flash-latest"
API_KEY_ENV = "GEMINI_API_KEY"

SYSTEM_INSTRUCTION = """\
You are the intake and narration layer of Arka, a PV siting and sizing planner.

You never produce a number. Not an estimate, not a rule of thumb, not a
"roughly". Every energy, currency, area and carbon figure comes from calling a
tool. If you need a figure you do not have, call the tool that computes it. If
no tool can produce it, say plainly that it is unknown and what the user must
supply.

Benchmarks tiered 'gap' in the source data are empty on purpose. Ask the user
for them. Never fill one in from your own knowledge.

When you write prose, quote computed figures exactly as they were given to you
and list every numeral you used in `figures_used`.
"""

# Numerals that are never quantities: years, small counts, list ordinals.
_YEAR_RANGE = range(1900, 2101)
_NUMERAL = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class AgentError(RuntimeError):
    """The agent layer could not complete a turn."""


class NumberGuardError(AgentError):
    """Model prose contained a figure that no computation produced."""

    def __init__(self, offenders: list[str]) -> None:
        super().__init__(
            "narrative contains figures that did not come from a computation: "
            + ", ".join(offenders)
        )
        self.offenders = offenders


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def numerals(text: str) -> list[str]:
    """Every numeric token in a piece of prose, as written."""
    return _NUMERAL.findall(text)


def _as_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def collect_values(payload: Any, out: list[float] | None = None) -> list[float]:
    """Every number reachable inside a tool result, flattened."""
    out = [] if out is None else out
    if isinstance(payload, bool):
        return out
    if isinstance(payload, (int, float)) and math.isfinite(payload):
        out.append(float(payload))
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_values(value, out)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            collect_values(value, out)
    elif isinstance(payload, str):
        for token in numerals(payload):
            parsed = _as_float(token)
            if parsed is not None:
                out.append(parsed)
    return out


def verify_no_invented_numbers(
    text: str,
    computed: Iterable[float],
    rel_tol: float = 0.01,
) -> list[str]:
    """Numerals in `text` that do not trace back to a computed figure.

    Rounding for presentation is allowed, so a match within `rel_tol` counts.
    Years and integers up to 12 are ignored: they are dates, month counts and
    list positions, not quantities.
    """
    allowed = [v for v in computed if math.isfinite(v)]
    offenders: list[str] = []
    for token in numerals(text):
        value = _as_float(token)
        if value is None:
            continue
        if value.is_integer() and (abs(value) <= 12 or int(value) in _YEAR_RANGE):
            continue
        if any(_matches(value, target, rel_tol) for target in allowed):
            continue
        offenders.append(token)
    return offenders


def _matches(value: float, target: float, rel_tol: float) -> bool:
    if math.isclose(value, target, rel_tol=rel_tol, abs_tol=1e-9):
        return True
    # Presentation layers rescale: 1,234,567 kWh shown as 1.23 GWh, 0.131 as 131.
    for factor in (1e-3, 1e3, 1e-2, 1e2, 1e-6, 1e6):
        if math.isclose(value, target * factor, rel_tol=rel_tol, abs_tol=1e-9):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------


def tool_declarations() -> list[Any]:
    """The registry in `tools.py`, handed to the SDK as callable functions.

    google-genai accepts plain Python callables and derives the schema from
    their signatures and docstrings, so the registry is the single source of
    truth for what the model can do.
    """
    return list(tools.REGISTRY.values())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """One exchange, with everything needed to audit it afterwards."""

    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    offenders: list[str] = field(default_factory=list)

    @property
    def computed_values(self) -> list[float]:
        values: list[float] = []
        for result in self.tool_results:
            collect_values(result, values)
        return values


class ArkaAgent:
    """Thin wrapper over google-genai. All figure production happens in `tools`."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get(API_KEY_ENV)
        self._client: Any | None = None

    @property
    def available(self) -> bool:
        """True when an API key is configured. The app runs fully without one."""
        return bool(self._api_key)

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise AgentError(
                f"no API key: set {API_KEY_ENV} in the environment or in "
                ".streamlit/secrets.toml. Every screen works without the agent."
            )
        try:
            from google import genai  # noqa: PLC0415 — lazy so the app runs without the SDK
        except ImportError as exc:  # pragma: no cover - depends on install
            raise AgentError("google-genai is not installed; pip install -r requirements.txt") from exc
        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _config(self, *, response_schema: Any | None = None, with_tools: bool = False) -> Any:
        from google.genai import types  # noqa: PLC0415

        kwargs: dict[str, Any] = {"system_instruction": SYSTEM_INSTRUCTION, "temperature": 0.0}
        if with_tools:
            kwargs["tools"] = tool_declarations()
            # Automatic function calling would have the SDK invoke the tools itself
            # and hand back only prose. The results would never pass through
            # `tools.call`, so `Turn.tool_results` would be empty and the number
            # guard would have nothing to check the prose against — it would then
            # flag every legitimate figure as invented. Keep execution on our side
            # of the line so the audit trail survives.
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True, maximum_remote_calls=None
            )
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema
        return types.GenerateContentConfig(**kwargs)

    def parse_intake(self, message: str) -> Intake:
        """Turn free text into a structured intake. No figures are produced."""
        client = self._connect()
        response = client.models.generate_content(
            model=self.model,
            contents=message,
            config=self._config(response_schema=Intake),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, Intake):
            return parsed
        return Intake.model_validate_json(response.text)

    def orchestrate(self, message: str) -> Turn:
        """Let the model choose deterministic tools and report what it ran."""
        client = self._connect()
        response = client.models.generate_content(
            model=self.model,
            contents=message,
            config=self._config(with_tools=True),
        )
        turn = Turn(text=getattr(response, "text", "") or "")
        for call in _function_calls(response):
            name = call.get("name")
            args = call.get("args") or {}
            turn.tool_calls.append((name, args))
            try:
                turn.tool_results.append(tools.call(name, args))
            except Exception as exc:
                # A tool that raises is a fact about this turn, not the end of it.
                # Record it so the caller can see what was attempted and why it
                # failed, and so one bad call does not discard the good ones.
                turn.tool_results.append(
                    {"tool": name, "arguments": args, "error": f"{type(exc).__name__}: {exc}"}
                )
        turn.offenders = verify_no_invented_numbers(turn.text, turn.computed_values)
        return turn

    def write_narrative(self, figures: dict[str, Any], brief: str = "") -> Narrative:
        """Prose for the report, screened before it is returned.

        `figures` is the computed result set. Anything numeric in the prose that
        is not traceable to it raises rather than reaching the report.
        """
        client = self._connect()
        prompt = (
            "Write the narrative for this PV appraisal. Use only the figures given. "
            "Quote them exactly.\n\n"
            f"Figures:\n{figures}\n\nBrief: {brief}"
        )
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=self._config(response_schema=Narrative),
        )
        parsed = getattr(response, "parsed", None)
        narrative = parsed if isinstance(parsed, Narrative) else Narrative.model_validate_json(response.text)
        offenders = verify_no_invented_numbers(
            narrative.headline + "\n" + narrative.body, collect_values(figures)
        )
        if offenders:
            raise NumberGuardError(offenders)
        return narrative


def _function_calls(response: Any) -> list[dict[str, Any]]:
    """Pull function calls out of a response, tolerating SDK shape changes."""
    calls: list[dict[str, Any]] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            fn = getattr(part, "function_call", None)
            if fn is not None and getattr(fn, "name", None):
                calls.append({"name": fn.name, "args": dict(getattr(fn, "args", {}) or {})})
    return calls

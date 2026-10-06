"""AgentCore Platform v1.0"""

# src/schemas/caller_contract.py — the single definition of what a caller may send.
#
# Both the HTTP adapter (src/api/server.py) and the validation node
# (src/nodes/input_validate_node.py) screen the caller's request through the
# helpers here, so the two boundaries can never drift apart: one place decides
# what an identifier is, what a number is, and what a hostile string looks like.
#
# Three properties this module exists to guarantee:
#
#   Inert.        Every caller string that can reach the rendered alert is locked
#                 to a bounded identifier alphabet. Free text there would be
#                 caller-controlled output injection — the alert is read by a
#                 human deciding whether to act on an engagement.
#   Finite.       Every caller number is parsed through a finite + bounded check.
#                 NaN and Infinity survive float(), and NaN compares False against
#                 every threshold, so an unchecked non-finite value silently
#                 disables the exact decision the agent exists to make.
#   Fail-closed.  An unrecognised shape is rejected, never coerced, and the
#                 rejection names the FIELD and never the value.

import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials_in_value

# ── Identifiers ───────────────────────────────────────────────────────────────

# The alphabet every caller string that renders into the alert is locked to.
# Chosen by reading the renderer rather than assumed: engagement labels, billing
# periods and metric names are the only caller strings that reach the output, and
# they render alongside generated alert ids of the same shape. Nothing here can
# open a markup token, start a new line, or introduce whitespace, so a caller
# cannot forge report structure through a value.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Metric names render as-is inside finding text, so they carry the tighter,
# lower-case form conventional for a metric key.
_METRIC_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# ── Injection screen ──────────────────────────────────────────────────────────

# Chat-template control markers, screened as a CLASS rather than as a list of
# phrases. A directive can be spelled in any words, but a payload that wants a
# model to change roles has to open one of these frames, and a phrase-only screen
# misses all of them.
_CONTROL_MARKERS: Tuple[str, ...] = (
    "<|",
    "|>",
    "[inst]",
    "[/inst]",
    "<<sys>>",
    "<</sys>>",
    "<|im_start|>",
    "<|im_end|>",
)

# Directive phrases, anchored on the object they act upon. Anchoring is what keeps
# the screen off legitimate professional-services language: an engagement note may
# well say "act as the settlement agent", and an unanchored verb pattern would
# refuse real work. Every pattern below needs both the verb and the thing it
# overrides before it fires.
_DIRECTIVE_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|preceding|earlier|all|any)\b[^.\n]{0,40}?"
        r"\b(?:instruction|instructions|rule|rules|direction|directions|prompt|prompts)\b"
    ),
    re.compile(
        r"\b(?:reveal|print|repeat|output|show)\b[^.\n]{0,30}?"
        r"\b(?:system|developer)\b[^.\n]{0,20}?\b(?:prompt|instructions|message)\b"
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b"),
    re.compile(r"\bnew\s+(?:system\s+)?(?:instructions|rules)\s*[:=]"),
)

_MARKUP_RE = re.compile(r"<[^<>]{0,80}>")


def _normalise(text: str) -> str:
    """Fold a string to the form the screens compare against.

    Unicode compatibility folding first, so a full-width or otherwise decorated
    spelling of a marker cannot walk past a plain substring test.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def _screen_one_string(text: str) -> Optional[str]:
    """Return the class of hostile content found in one string, or None.

    The string is screened TWICE. Raw first: that is the only pass that can still
    see a control marker, because stripping markup deletes the marker and forwards
    the directive behind it as ordinary text — turning a detectable attack into an
    undetectable one. Stripped second: that is the only pass that can see a
    directive spliced apart by inserted tags.
    """
    for candidate in (_normalise(text), _normalise(_MARKUP_RE.sub("", text))):
        for marker in _CONTROL_MARKERS:
            if marker in candidate:
                return "control_marker"
        for pattern in _DIRECTIVE_RES:
            if pattern.search(candidate):
                return "directive_phrase"
    return None


def _safe_path(path: str) -> str:
    """Render a state path for an error message.

    A field NAME is caller data too, so a component that is not itself an inert
    identifier is replaced rather than echoed.
    """
    parts: List[str] = []
    for index, part in enumerate(path.split(".")):
        if _IDENTIFIER_RE.match(part) and len(part) <= 64:
            parts.append(part)
        else:
            parts.append(f"field#{index}")
    return ".".join(parts)


def screen_injection(value: Any, path: str = "payload") -> Optional[str]:
    """Depth-first screen of every string in a parsed payload, KEYS included.

    Scanning after the parse is what makes escaped spellings irrelevant: a
    ``\\u003c|im_start|\\u003e`` in the wire form is an ordinary marker by the time
    it is a Python string. Returns a location-only description of the first
    finding, or None. Fails closed: the caller of this function rejects the
    request on any non-None result.
    """
    if isinstance(value, str):
        found = _screen_one_string(value)
        return f"{_safe_path(path)} ({found})" if found else None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found = _screen_one_string(key)
                if found:
                    return f"{_safe_path(path)} (key, {found})"
            child = screen_injection(item, f"{path}.{key}")
            if child:
                return child
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = screen_injection(item, f"{path}[{index}]")
            if child:
                return child
    return None


def screen_credentials(context: Dict[str, Any]) -> Optional[str]:
    """Name the first field of a context mapping that carries a credential shape.

    Delegates to the framework's own detector, so this refusal set is exactly the
    framework's block set — a locally invented pattern list narrower than the
    framework's is a bypass, and one wider than it refuses requests the platform
    would have served.

    Iterating the top-level fields is equivalent to scanning the whole mapping —
    ``detect_credentials_in_value`` on a mapping is defined as the union over its
    values — and that equivalence is what allows the refusal to name a field
    without widening or narrowing what is refused.

    The returned string is a field name only. The matched text is never echoed.
    """
    for index, (name, value) in enumerate(context.items()):
        if detect_credentials_in_value(value):
            safe = (
                name if isinstance(name, str) and _IDENTIFIER_RE.match(name) and not _screen_one_string(name) else None
            )
            return f"input_context.{safe}" if safe else f"input_context field #{index}"
    return None


# ── Numbers ───────────────────────────────────────────────────────────────────


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Return the value as a float when it is a real, finite number within bounds.

    Returns None otherwise — bools (which are ints in Python and would sail
    through an isinstance check), strings, None, NaN and ±Infinity all land here.
    Rejecting non-finite values is the point of the function: ``float("NaN")``
    parses, and every comparison against it is False, so a NaN metric would be
    reported as "within normal parameters" rather than refused.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def is_identifier(value: Any, max_length: int) -> bool:
    """True when the value is an inert, bounded identifier string."""
    return isinstance(value, str) and 1 <= len(value) <= max_length and bool(_IDENTIFIER_RE.match(value))


def is_metric_name(value: Any, max_length: int) -> bool:
    """True when the value is an inert, bounded metric key."""
    return isinstance(value, str) and 1 <= len(value) <= max_length and bool(_METRIC_NAME_RE.match(value))


# ── Field bounds ──────────────────────────────────────────────────────────────

# Per-metric bounds. A metric the caller invents is bounded by the generic pair;
# a metric the pipeline reasons about specifically is bounded to its own realistic
# range so an out-of-contract figure is refused rather than analysed.
METRIC_BOUNDS: Dict[str, Tuple[float, float]] = {
    "billed_hours": (0.0, 1_000_000.0),
    "utilization_rate": (0.0, 10.0),
    "revenue_jpy": (0.0, 1e13),
    "sla_breach_count": (0.0, 100_000.0),
    "expense_ratio": (0.0, 10.0),
    "team_size": (0.0, 100_000.0),
}

GENERIC_METRIC_BOUNDS: Tuple[float, float] = (-1e12, 1e12)

# Required top-level keys of the operations payload.
REQUIRED_PAYLOAD_KEYS: Tuple[str, ...] = ("engagement_id", "billing_period", "metrics")

# Required metric keys inside the nested metrics mapping.
REQUIRED_METRIC_KEYS: Tuple[str, ...] = ("billed_hours", "utilization_rate", "revenue_jpy")


def metric_bounds(name: str) -> Tuple[float, float]:
    """Bounds for one metric name."""
    return METRIC_BOUNDS.get(name, GENERIC_METRIC_BOUNDS)

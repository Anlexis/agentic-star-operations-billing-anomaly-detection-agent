"""AgentCore Platform v1.0"""

# SVC-C2-010 — OutputFormatNode
# Outer backbone post_process node: render the management alert and enforce the
# output boundary before anything reaches the caller.
#
# Two independent layers run here, each with its own audit event:
#
#   credential scan   delegated to the framework's own detector, so this gate's
#                     block set is exactly the platform's. A local pattern list
#                     narrower than the framework's is a bypass rather than a
#                     defence: the value passes this gate, the platform raises
#                     later inside post-processing, and the partial result that
#                     surfaces then is not the one this node withheld.
#   precision grid    every monetary figure the report renders is an aggregate on
#                     the nearest-1,000 grid. The pipeline already rounds amounts
#                     where it builds them, so on the normal path this layer finds
#                     nothing; it is the boundary that makes the stated rule true
#                     of the rendered text rather than of the intention.
#
# ORDER MATTERS. The credential scan runs BEFORE the numeric snap. The snap reads
# any standalone three-letter uppercase word as a currency marker, so running it
# first would rewrite the digits of a pattern the credential scan is looking for
# and the scan would then find nothing to block.
#
# On a violation this node does not raise. The framework's output envelope is
# `formatted_output or result`, with no status check, so an error that leaves the
# answer fields populated ships the un-gated answer inside the error envelope.
# The gate therefore returns ERROR, clears every output-bearing field, and puts a
# TRUTHY refusal notice in formatted_output — a falsy one would re-open the very
# fallback the clearing exists to close.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.nodes.classify_anomaly_node import MONETARY_ROUND_UNIT

logger = logging.getLogger(__name__)

# Text placed in formatted_output when the gate withholds the report. Truthy on
# purpose, and content-free: it states that nothing was released and names no
# value, no field of the withheld report and no source location.
WITHHELD_NOTICE = (
    "Alert withheld: the generated report did not satisfy the output policy "
    "for this agent and was not released. Re-run the request or contact the "
    "operations owner if this persists."
)

# Closed set of reasons a report can be withheld. Only these labels ever reach a
# log line, so a reason can never carry caller text or matched content.
REASON_CREDENTIAL = "credential_pattern_in_report"
REASON_RENDER_FAILED = "report_render_failed"
_REASONS = frozenset({REASON_CREDENTIAL, REASON_RENDER_FAILED})

# Every state field that can carry answer text or a payload. The gate blanks all
# of them together, so a checkpoint reader or a later node cannot pick up what the
# caller was refused. Kept as one named inventory so a new output-bearing field
# cannot quietly join the state without joining this set — the test suite pins the
# two against each other.
OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "management_alert",
    "operations_data",
    "anomaly_classifications",
    "deviations",
    "baseline_data",
    "baseline_sources",
    "unbaselined_metrics",
    "caller_payload",
    "enriched_context",
    "result",
)


# ── Precision grid ────────────────────────────────────────────────────────────

# This report's render alphabet, read off the renderer rather than assumed:
# engagement labels, billing periods, metric names and generated alert ids are
# all `[A-Za-z0-9_-]`. A monetary token never begins or ends inside one of those,
# so the whole grammar is wrapped in a pair of fixed-width guards. The LEADING
# guard also carries `.`, so no alternative can enter a number part-way through
# and treat the tail of a decimal fraction as a value of its own; `.` is
# deliberately absent from the TRAILING guard, or an amount ending a sentence
# would escape the grid.
_IDENT_CHAR = r"A-Za-z0-9_\-"
_LEAD_GUARD = rf"(?<![{_IDENT_CHAR}.])"
_TRAIL_GUARD = rf"(?![{_IDENT_CHAR}])"

_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"

# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so
# a three-letter uppercase word ending a line would bind to the number that opens
# the next block and rewrite it ("Currency: JPY\n\n3. Recommended Actions" ->
# "0. Recommended Actions"), i.e. the gate would rewrite document structure.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a decimal part, and every value alternative absorbs
# it into the SAME token. Without that, the fraction of "9999.99999" is a
# standalone five-digit run in its own right and gets rewritten into a number the
# report never contained; and in currency context the integer part snaps while the
# fraction dangles ("JPY 1234.56" -> "JPY 1,000.56").
#
# The `(?!\.\d)` arm is what makes the absorption stick. A plain `(?:\.\d+)?` lets
# the engine backtrack out of the fraction and re-match the integer part alone
# whenever the text right after the fraction fails the trailing guard, and the
# dangling-fraction bug returns. Either the fraction is taken whole, or there is
# none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999".
    # The value alternatives take the comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1" and
    # the snap would mangle the number instead of rounding it.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)

# ISO 4217 alphabetic codes, used for ONE decision: whether
# "<three uppercase letters>-<digits>" is a negative amount or an identifier. The
# two are lexically identical — "JPY-9999" (a signed amount, a real leak form) and
# "ENG-2026" (an engagement label) have the same shape — so no amount of guard
# widening separates them; something has to know which three-letter words are
# currencies. ISO 4217 is a closed, standardised vocabulary, unlike the open set
# of identifiers, which is why the knowledge sits on this side. Everywhere else
# the grammar still reads any standalone three-letter uppercase word as a marker,
# because a false snap there fails safe. Only the ATTACHED form is narrowed: a
# separated marker ("ENG 6205") still snaps, so the ambiguous case stays fail-safe.
_ISO_CURRENCY_CODES = frozenset(
    "AED AUD BRL CAD CHF CNY DKK EUR GBP HKD IDR ILS INR JPY KRW MXN MYR NOK NZD "
    "PHP PLN RUB SAR SEK SGD THB TRY TWD USD VND ZAR".split()
)

SCHEMA_NOTE = (
    "Monetary figures are reported as aggregates rounded to the nearest 1,000; "
    "individual line items are not reported."
)


def _is_identifier_hyphen(match: "re.Match[str]") -> bool:
    """True when this match is "<letters>-<digits>", i.e. an identifier.

    Only the marker-then-value branch with an ALPHABETIC marker, an EMPTY
    delimiter and a signed value can be ambiguous; every other form is
    unambiguous and never reaches this test.
    """
    pre = match.group("pre") or ""
    token = match.group("val_after") or ""
    if not pre or not token.startswith(("-", "+")):
        return False
    marker = pre.strip()
    if not marker.isalpha():  # a currency SYMBOL is never an identifier prefix
        return False
    return pre == marker and marker.upper() not in _ISO_CURRENCY_CODES


def enforce_precision(text: str) -> Tuple[str, int]:
    """Snap every monetary-form token onto the reported grid.

    Returns (text, snap_count). A snap means a full-precision figure reached the
    external surface and the gate rounded it. The currency marker, the original
    delimiter whitespace and the explicit sign of the original token are all
    preserved on the replacement.
    """
    snaps = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal snaps
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        if _is_identifier_hyphen(match):
            return match.group(0)
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))
        if value % MONETARY_ROUND_UNIT == 0:
            return match.group(0)
        snaps += 1
        snapped = round(value / MONETARY_ROUND_UNIT) * MONETARY_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, text), snaps


# ── Rendering ─────────────────────────────────────────────────────────────────


def _format_severity_banner(severity: str) -> str:
    banners = {
        "critical": "[CRITICAL ALERT]",
        "high": "[HIGH ALERT]",
        "medium": "[MEDIUM ALERT]",
        "normal": "[STATUS NORMAL]",
    }
    return banners.get(severity, "[ALERT]")


def format_alert(alert: Dict[str, Any]) -> str:
    """Render the management alert into the human-readable report."""
    severity = str(alert.get("severity", "normal"))
    banner = _format_severity_banner(severity)
    lines: List[str] = [
        f"{banner} — Engagement: {alert.get('engagement_id', 'N/A')}",
        f"Period: {alert.get('billing_period', 'N/A')}  |  " f"Alert ID: {alert.get('alert_id', 'N/A')}",
        "",
        str(alert.get("summary", "")),
        "",
    ]

    findings: List[Dict[str, Any]] = list(alert.get("findings") or [])
    if findings:
        lines.append("Findings:")
        for index, finding in enumerate(findings, 1):
            lines.append(
                f"  {index}. [{str(finding.get('severity', '')).upper()}] "
                f"{finding.get('anomaly_type', '')} — {finding.get('description', '')} "
                f"(baseline: {finding.get('baseline_source', 'benchmark')})"
            )
        lines.append("")

    unanalysed: List[str] = list(alert.get("unanalysed_metrics") or [])
    if unanalysed:
        lines.append("Not analysed (no baseline available): " + ", ".join(unanalysed))
        lines.append("")

    actions: List[str] = list(alert.get("recommended_actions") or [])
    if actions:
        lines.append("Recommended Actions:")
        for index, action in enumerate(actions, 1):
            lines.append(f"  {index}. {action}")
        lines.append("")

    lines.append(SCHEMA_NOTE)
    return "\n".join(lines).strip()


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class OutputFormatNode(FunctionNode):
    """Render the management alert and enforce the output boundary.

    Outer backbone post_process slot. Declared ANONYMOUS: the trust decision was
    made at InputValidateNode, and an output gate that could itself be skipped on
    a trust mismatch would be no gate at all.

    Input state keys:
        management_alert: dict — from GenerateManagementAlertNode via merge_output
        alert_severity:   str
        operations_data:  dict

    Output state keys (partial dict):
        formatted_output: str  — the report, or the withheld notice
        status:           str
        plus every field of OUTPUT_BEARING_FIELDS, blanked, on a violation
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run the caller-contract gate declined produced no alert to render.
        # Without this branch the "no alert in state" path below reports
        # "Operations are within normal parameters" - a clean-baseline result for
        # a request that was never analysed at all.
        marker = state.get("error_code")
        if marker:
            emit_trace_event("output_not_produced", {"reason": marker}, state)
            return {
                "formatted_output": _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED),
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        management_alert: Dict[str, Any] = state.get("management_alert") or {}
        alert_severity: str = str(state.get("alert_severity") or "normal")
        operations_data: Dict[str, Any] = state.get("operations_data") or {}
        engagement_id = operations_data.get("engagement_id", "unknown")

        if not management_alert:
            # No alert was produced: report the analysed-clean baseline rather
            # than an empty response.
            logger.info("OutputFormatNode: no management alert in state — reporting normal status")
            management_alert = {
                "severity": "normal",
                "summary": "No anomalies detected. Operations are within normal parameters.",
                "findings": [],
                "recommended_actions": [],
                "unanalysed_metrics": [],
                "engagement_id": engagement_id,
                "billing_period": operations_data.get("billing_period", "unknown"),
                "alert_id": "N/A",
                "anomaly_count": 0,
            }

        try:
            rendered = format_alert(management_alert)
        except (TypeError, ValueError):
            return self._withhold(state, REASON_RENDER_FAILED)

        # Layer 1 — credential scan, BEFORE the numeric snap (see module header).
        if detect_credentials(rendered):
            return self._withhold(state, REASON_CREDENTIAL)

        # Layer 2 — precision grid.
        rendered, snaps = enforce_precision(rendered)
        if snaps:
            emit_trace_event(
                "output_precision_enforced",
                {"engagement_id": engagement_id, "snapped_values": snaps},
                state,
            )

        anomaly_count = management_alert.get("anomaly_count", 0)
        logger.info(
            "OutputFormatNode: engagement=%s severity=%s output_chars=%d",
            engagement_id,
            alert_severity,
            len(rendered),
        )
        emit_trace_event(
            "output_formatted",
            {
                "engagement_id": engagement_id,
                "alert_severity": alert_severity,
                "anomaly_count": anomaly_count,
                "output_length": len(rendered),
            },
            state,
        )

        return {
            "formatted_output": rendered,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── Containment ───────────────────────────────────────────────────────────

    def _withhold(self, state: AgentState, reason: str) -> Dict[str, Any]:
        """Refuse the report: ERROR, every output-bearing field blanked, notice set.

        The returned delta names each cleared key explicitly. Omitting a key would
        leave the previous value in state — deltas are merged, not replaced — and
        a test asserting the field is falsy would then pass on a gate that cleared
        nothing.
        """
        safe_reason = reason if reason in _REASONS else "output_policy"
        logger.warning("OutputFormatNode: report withheld — %s", safe_reason)
        emit_trace_event("output_withheld", {"reason": safe_reason}, state)

        cleared: Dict[str, Any] = {field: None for field in OUTPUT_BEARING_FIELDS}
        cleared["formatted_output"] = WITHHELD_NOTICE
        cleared["status"] = AgentStatus.ERROR.value
        cleared["error_log"] = [f"OutputFormatNode: report withheld ({safe_reason})"]
        return cleared

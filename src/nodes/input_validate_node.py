"""AgentCore Platform v1.0"""

# SVC-C2-010 — InputValidateNode
# Outer backbone pre_process node: the caller-data contract and the trust gate.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level)
#   - Read the operations payload from the structured request channel, or from
#     the request body as a JSON fallback
#   - Screen the whole parsed payload — values AND keys — for hostile content
#   - Bind every caller string to an inert identifier and every caller number to
#     a finite, bounded range, refusing anything else
#   - Write caller_payload + a short inert validated_input summary
#   - Emit an audit event for every validation decision
#
# Returns ONLY the state keys this node writes (partial-dict contract).
#
# Refusals name the FIELD and never the value: a rejected value is caller data of
# unknown provenance and echoing it back into a log or an error envelope hands it
# to whoever reads that surface.

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.services.progress import emit_progress
from src.schemas.caller_contract import (
    REQUIRED_METRIC_KEYS,
    REQUIRED_PAYLOAD_KEYS,
    finite_in_range,
    is_identifier,
    is_metric_name,
    metric_bounds,
    screen_injection,
)

logger = logging.getLogger(__name__)

# Key of the structured request channel that carries the operations payload.
PAYLOAD_CONTEXT_KEY = "operations_payload"

# Bounds used when config/config.yaml declares nothing usable.
_DEFAULT_MAX_METRICS = 32
_DEFAULT_MAX_IDENTIFIER = 32

# Upper bound on the JSON fallback body, in characters. The structured channel is
# bounded at the adapter; this bounds the other entry.
_MAX_BODY_CHARS = 262_144


def _contract_bounds(state: AgentState) -> Tuple[int, int]:
    """Read the caller-contract bounds the graph seeded into state."""
    settings = state.get("runtime_settings") or {}
    if not isinstance(settings, dict):
        return _DEFAULT_MAX_METRICS, _DEFAULT_MAX_IDENTIFIER
    max_metrics = settings.get("max_metrics", _DEFAULT_MAX_METRICS)
    max_identifier = settings.get("max_identifier_length", _DEFAULT_MAX_IDENTIFIER)
    return (
        int(max_metrics) if isinstance(max_metrics, int) and max_metrics > 0 else _DEFAULT_MAX_METRICS,
        int(max_identifier) if isinstance(max_identifier, int) and max_identifier > 0 else _DEFAULT_MAX_IDENTIFIER,
    )


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


def _reject(state: AgentState, reason: str, message: str, code: str = "INVALID_REQUEST") -> Dict[str, Any]:
    """Build the refusal delta and record the decision.

    Two outcomes, chosen by whether the caller can act on the finding, and
    selected by an explicit argument at the call site rather than by the text of
    `reason` or `message` — so the distinction survives any later rewording.

    `code` non-empty — a value the caller can correct (nothing sent, too long,
    out of contract). The run COMPLETES carrying the reason, so the calling
    surface can show the sentence and the caller can send a corrected request on
    the same conversation instead of receiving only an exception type.

    `code` empty — a refusal the caller cannot reword their way past (hostile
    content). This terminates, exactly as before.

    Either way the payload is NOT accepted and nothing is published; only the way
    the refusal is reported changes. `message` names fields and bounds and stays
    in the audit channel — the caller-facing sentence names none of them.
    """
    emit_trace_event("input_validation_failed", {"reason": reason}, state)
    logger.warning("InputValidateNode: rejected — %s", reason)
    if code:
        emit_progress(INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"InputValidateNode: {message}"],
            "formatted_output": _DEGRADED_MESSAGES.get(code, INPUT_REJECTED),
        }
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"InputValidateNode: {message}"],
    }


def _read_payload(state: AgentState) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, str, str]]]:
    """Return the raw payload mapping, or a (reason, message, code) refusal.

    The third element is the reason code. "Nothing was sent" and "what was sent
    is too long to read" are different things to correct, and collapsing both
    into the generic code would tell the caller to check a format when the fix is
    to send a payload, or to shorten one.

    Preferred channel: the structured request context, which the platform does
    not rewrite. Fallback: the request body parsed as JSON, kept so an existing
    caller keeps working — text on that channel is subject to the platform's
    input masking, which is why the structured channel is preferred.
    """
    context = state.get("input_context") or {}
    if isinstance(context, dict):
        candidate = context.get(PAYLOAD_CONTEXT_KEY)
        if isinstance(candidate, dict):
            return candidate, None
        if candidate is not None:
            return None, ("context_payload_not_object", f"'{PAYLOAD_CONTEXT_KEY}' must be an object", "INVALID_REQUEST")

    body = state.get("user_input", "")
    if not isinstance(body, str) or not body.strip():
        return None, ("empty_input", "no operations payload was supplied", "EMPTY_INPUT")
    if len(body) > _MAX_BODY_CHARS:
        # The whole request is oversized, not one field of it.
        return None, (
            "body_too_large",
            f"request body must be {_MAX_BODY_CHARS} characters or fewer",
            "QUESTION_TOO_LONG",
        )
    try:
        parsed = json.loads(body.strip())
    except (json.JSONDecodeError, ValueError):
        return None, ("json_parse_error", "request body is not valid JSON", "INVALID_REQUEST")
    if not isinstance(parsed, dict):
        return None, ("payload_not_object", "the operations payload must be a JSON object", "INVALID_REQUEST")
    return parsed, None


class InputValidateNode(FunctionNode):
    """The caller-data contract for SVC-C2-010.

    This is the outer backbone's pre_process slot and the only node in the
    template with VERIFIED_EXTERNAL trust, so an unauthenticated caller is
    refused here. The inner graph nodes carry ANONYMOUS trust and never see an
    unvetted payload.

    The screens run in this node rather than being left to the platform. A
    template that relies on the platform's input gate alone is fail-OPEN wherever
    that gate is absent or configured off: the payload reaches the answer path and
    the agent returns a successful-looking report.

    Input state keys:
        input_context: dict — structured request; ``operations_payload`` carries
            the operations mapping
        user_input:    str  — JSON body, used when the structured channel is absent

    Output state keys (partial dict):
        caller_payload:   dict      — the accepted, bounded payload
        validated_input:  str       — short inert summary of what was accepted
        enriched_context: dict      — channel metadata for traceability
        status:           str
        error_log:        list[str] — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        max_metrics, max_identifier = _contract_bounds(state)

        payload, refusal = _read_payload(state)
        if refusal is not None:
            return _reject(state, refusal[0], refusal[1], code=refusal[2])
        assert payload is not None  # narrowed by the refusal branch above

        # ── Hostile-content screen, before any field is interpreted ───────────
        finding = screen_injection(payload)
        if finding:
            # code="" keeps this one terminal. Spliced instructions are not a
            # value the caller can correct by rewording, and completing the run
            # would make a refusal read like an ordinary declined request.
            return _reject(
                state,
                "hostile_content",
                f"request refused: disallowed content at {finding}",
                code="",
            )

        # ── Required fields ───────────────────────────────────────────────────
        missing_top = [key for key in REQUIRED_PAYLOAD_KEYS if key not in payload]
        if missing_top:
            return _reject(
                state,
                "missing_required_fields",
                f"missing required fields: {sorted(missing_top)}",
            )

        # ── Identifiers: inert and bounded ────────────────────────────────────
        for field in ("engagement_id", "billing_period"):
            if not is_identifier(payload.get(field), max_identifier):
                return _reject(
                    state,
                    "identifier_out_of_contract",
                    f"'{field}' must be 1-{max_identifier} characters of A-Z a-z 0-9 _ -",
                )

        # ── Metrics ───────────────────────────────────────────────────────────
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            return _reject(state, "metrics_not_object", "'metrics' must be a non-empty object")
        if len(metrics) > max_metrics:
            return _reject(
                state,
                "too_many_metrics",
                f"'metrics' must carry {max_metrics} entries or fewer",
            )

        missing_metrics = [key for key in REQUIRED_METRIC_KEYS if key not in metrics]
        if missing_metrics:
            return _reject(
                state,
                "missing_metric_keys",
                f"missing required metric fields: {sorted(missing_metrics)}",
            )

        clean_metrics, metric_refusal = self._clean_numeric_map(metrics, "metrics", max_identifier)
        if metric_refusal is not None:
            return _reject(state, metric_refusal[0], metric_refusal[1])

        # ── Optional caller-supplied historical baselines ─────────────────────
        clean_baselines: Optional[Dict[str, float]] = None
        raw_baselines = payload.get("baseline_metrics")
        if raw_baselines is not None:
            if not isinstance(raw_baselines, dict):
                return _reject(state, "baselines_not_object", "'baseline_metrics' must be an object")
            if len(raw_baselines) > max_metrics:
                return _reject(
                    state,
                    "too_many_baselines",
                    f"'baseline_metrics' must carry {max_metrics} entries or fewer",
                )
            clean_baselines, baseline_refusal = self._clean_numeric_map(
                raw_baselines, "baseline_metrics", max_identifier
            )
            if baseline_refusal is not None:
                return _reject(state, baseline_refusal[0], baseline_refusal[1])

        # ── Optional team size ────────────────────────────────────────────────
        team_size: Optional[float] = None
        if payload.get("team_size") is not None:
            lo, hi = metric_bounds("team_size")
            team_size = finite_in_range(payload.get("team_size"), lo, hi)
            if team_size is None:
                return _reject(
                    state,
                    "team_size_out_of_contract",
                    f"'team_size' must be a finite number between {lo:g} and {hi:g}",
                )

        engagement_id = str(payload["engagement_id"])
        billing_period = str(payload["billing_period"])

        accepted: Dict[str, Any] = {
            "engagement_id": engagement_id,
            "billing_period": billing_period,
            "metrics": clean_metrics,
            "baseline_metrics": clean_baselines,
            "team_size": team_size,
        }

        # The framework-visible copy is a summary, not the payload: everything in
        # it is already an inert identifier or a count, so the platform's masking
        # has nothing to rewrite and the pipeline reads what the caller sent.
        summary = (
            f"engagement={engagement_id} period={billing_period} "
            f"metrics={len(clean_metrics)} baselines={len(clean_baselines or {})}"
        )

        request_context = state.get("input_context") or {}
        channel = "unknown"
        if isinstance(request_context, dict) and is_identifier(request_context.get("channel"), 32):
            channel = str(request_context["channel"])

        logger.info(
            "InputValidateNode: accepted engagement=%s metrics=%d baselines=%d",
            engagement_id,
            len(clean_metrics),
            len(clean_baselines or {}),
        )
        emit_trace_event(
            "input_validated",
            {
                "engagement_id": engagement_id,
                "billing_period": billing_period,
                "metric_count": len(clean_metrics),
                "caller_baseline_count": len(clean_baselines or {}),
            },
            state,
        )

        return {
            "caller_payload": accepted,
            "validated_input": summary,
            "enriched_context": {
                "source": "BillingAnomalyDetectionAgent",
                "channel": channel,
                "engagement_id": engagement_id,
            },
            "status": AgentStatus.SUCCESS.value,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean_numeric_map(
        raw: Dict[Any, Any], field: str, max_identifier: int
    ) -> Tuple[Dict[str, float], Optional[Tuple[str, str]]]:
        """Validate one caller-supplied {metric_name: number} mapping.

        Both halves are caller data: an out-of-contract KEY is refused before its
        value is looked at, because the key renders into the finding text.
        """
        clean: Dict[str, float] = {}
        bad_keys: List[str] = []
        for name, value in raw.items():
            if not is_metric_name(name, max_identifier):
                bad_keys.append(str(len(bad_keys)))
                continue
            lo, hi = metric_bounds(str(name))
            parsed = finite_in_range(value, lo, hi)
            if parsed is None:
                return clean, (
                    "metric_out_of_contract",
                    f"'{field}.{name}' must be a finite number between {lo:g} and {hi:g}",
                )
            clean[str(name)] = parsed
        if bad_keys:
            return clean, (
                "metric_name_out_of_contract",
                f"'{field}' carries {len(bad_keys)} key(s) outside a-z 0-9 _ " f"of 1-{max_identifier} characters",
            )
        return clean, None

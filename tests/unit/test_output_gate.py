# Unit tests for the output boundary: the precision grid, the credential scan,
# and what the envelope carries when the gate withholds a report.

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.output_format_node import (
    OUTPUT_BEARING_FIELDS,
    REASON_CREDENTIAL,
    WITHHELD_NOTICE,
    OutputFormatNode,
    enforce_precision,
    format_alert,
)

ALERT = {
    "alert_id": "ALERT-ENG-2026-001-AB12CD34",
    "engagement_id": "ENG-2026-001",
    "billing_period": "2026-Q2",
    "severity": "high",
    "anomaly_count": 1,
    "summary": "HIGH-severity alert for engagement ENG-2026-001 — 1 anomaly detected.",
    "findings": [
        {
            "anomaly_type": "revenue_underperform",
            "severity": "high",
            "metric": "revenue_jpy",
            "deviation_pct": -31.2,
            "baseline_source": "benchmark",
            "description": "Revenue is 31.2% below baseline (current: JPY 52,000,000, "
            "expected JPY 76,000,000). Investigate pricing or write-off.",
        }
    ],
    "recommended_actions": ["Compare billable vs. billed hours to detect write-offs."],
    "unanalysed_metrics": [],
}


def _state(**extra):
    state = {
        "management_alert": dict(ALERT),
        "alert_severity": "high",
        "operations_data": {"engagement_id": "ENG-2026-001", "billing_period": "2026-Q2", "metrics": {}},
        "anomaly_classifications": [{"metric": "revenue_jpy"}],
        "deviations": [{"metric": "revenue_jpy"}],
        "baseline_data": {"revenue_jpy": 76000000.0},
        "baseline_sources": {"revenue_jpy": "benchmark"},
        "unbaselined_metrics": [],
        "caller_payload": {"engagement_id": "ENG-2026-001"},
        "enriched_context": {"engagement_id": "ENG-2026-001"},
        "error_log": [],
        "node_history": [],
        "correlation_id": "test-corr",
    }
    state.update(extra)
    return state


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.output_format_node.emit_trace_event", lambda *a, **k: None)


# ── Precision grid ────────────────────────────────────────────────────────────


class TestPrecisionGrid:
    """Every representation of an amount is snapped, not only the convenient ones."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("JPY 9999", "JPY 10,000"),
            ("9999 JPY", "10,000 JPY"),
            ("JPY-9999", "JPY-10,000"),
            ("JPY +9999", "JPY +10,000"),
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY\n9999", "JPY\n10,000"),
            ("¥9999", "¥10,000"),
            ("￥9999", "￥10,000"),
            ("9999円", "10,000円"),
            ("9999₩", "10,000₩"),
            ("$9999", "$10,000"),
            ("52000001", "52,000,000"),
            ("1,234,567", "1,235,000"),
            ("JPY 1234.56", "JPY 1,000"),
        ],
    )
    def test_leak_forms_snap(self, text, expected):
        assert enforce_precision(text)[0] == expected

    def test_no_magnitude_exemption(self):
        """ "ALL amounts" means all of them — a small figure is not exempt."""
        assert enforce_precision("JPY 9,999")[0] == "JPY 10,000"

    def test_grouped_value_snaps_as_a_whole_token(self):
        """Leftmost-first matching would otherwise take "JPY 1" out of "JPY 1,234"."""
        assert enforce_precision("JPY 1,234")[0] == "JPY 1,000"

    def test_a_value_already_on_the_grid_is_byte_identical(self):
        assert enforce_precision("JPY 1,000") == ("JPY 1,000", 0)

    def test_the_delimiter_stays_inside_one_line(self):
        """A blank-line-spanning delimiter lets the gate rewrite section numbers."""
        text = "Currency: JPY\n\n3. Recommended Actions"
        assert enforce_precision(text) == (text, 0)

    @pytest.mark.parametrize(
        "text",
        [
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            "current: 94%",
            "17.6% above baseline",
            "expected ~0.5",
        ],
    )
    def test_decimals_and_percentages_survive(self, text):
        assert enforce_precision(text) == (text, 0)

    def test_the_fraction_cannot_be_backtracked_out_of(self):
        """A plain (?:\\.\\d+)? re-matches the integer part and yields JPY 1,000.56m."""
        rendered, _ = enforce_precision("JPY 1234.56m")
        assert rendered != "JPY 1,000.56m"

    @pytest.mark.parametrize(
        "identifier",
        [
            "ENG-2026-001",
            "ENG-99999",
            "ALERT-ENG-2026-001-C596E559",
            "eng_48210",
            "sku_48210",
            "metric_2026_001",
            "billed_hours",
            "2026-Q2",
        ],
    )
    def test_this_report_s_identifiers_survive(self, identifier):
        """The guard class is this report's own render alphabet, not an assumption.

        A purely numeric run inside an identifier has no letters to protect it,
        which is why the guards cover `_` and `-` as well as the alphanumerics.
        """
        assert enforce_precision(identifier) == (identifier, 0)

    def test_a_separated_three_letter_marker_still_snaps(self):
        """Only the ATTACHED "<letters>-<digits>" form is narrowed.

        The separated case stays ambiguous, so the gate keeps failing safe there.
        """
        assert enforce_precision("ENG 6205")[1] == 1

    def test_a_currency_code_attached_to_a_negative_amount_still_snaps(self):
        assert enforce_precision("JPY-9999")[0] == "JPY-10,000"

    @pytest.mark.parametrize("text", ["90d horizon", "STAR 2026", "  1. Initiate SLA post-mortem"])
    def test_structural_tokens_are_byte_identical(self, text):
        assert enforce_precision(text) == (text, 0)

    def test_a_pattern_scan_still_sees_a_number_group_after_the_snap(self):
        """Layer order: the credential scan runs before the snap.

        The snap reads any standalone three-letter uppercase word as a marker, so
        running it first would rewrite the digits a pattern scan is looking for.
        These guards keep such a group intact either way.
        """
        assert enforce_precision("SSN 123-45-6789") == ("SSN 123-45-6789", 0)

    def test_the_rendered_report_states_the_rule_it_enforces(self):
        assert "nearest 1,000" in format_alert(dict(ALERT))


# ── Containment ───────────────────────────────────────────────────────────────


class TestWithholding:
    """A violating gate CLEARS the output-bearing fields — raising is not containment."""

    def _violating_state(self):
        alert = dict(ALERT)
        alert["findings"] = [
            dict(
                ALERT["findings"][0],
                description="Investigate token AKIAIOSFODNN7EXAMPLE in the billing export.",
            )
        ]
        return _state(management_alert=alert)

    def test_a_credential_in_the_report_is_withheld(self):
        result = OutputFormatNode().execute(self._violating_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert "AKIAIOSFODNN7EXAMPLE" not in str(result)

    def test_every_output_bearing_field_is_present_and_blank(self):
        """Presence AND emptiness.

        Deltas are merged, not replaced: a gate that omits a key leaves the old
        value in state, and an assertion that only checks falsiness would pass on
        a gate that cleared nothing.
        """
        result = OutputFormatNode().execute(self._violating_state())
        for field in OUTPUT_BEARING_FIELDS:
            assert field in result, f"{field} missing from the delta — old value survives the merge"
            assert not result[field], f"{field} still carries content"

    def test_the_replacement_notice_is_truthy(self):
        """A falsy formatted_output re-opens the framework's fallback to `result`."""
        result = OutputFormatNode().execute(self._violating_state())
        assert result["formatted_output"]
        assert result["formatted_output"] == WITHHELD_NOTICE

    def test_the_notice_carries_nothing_read_back_out_of_state(self):
        """Clearing and then rebuilding the envelope from the same state is
        containment in form only."""
        result = OutputFormatNode().execute(self._violating_state())
        notice = result["formatted_output"]
        for leaked in ("ENG-2026-001", "ALERT-ENG-2026-001-AB12CD34", "2026-Q2", "52,000,000"):
            assert leaked not in notice

    def test_the_reason_comes_from_a_closed_set(self):
        result = OutputFormatNode().execute(self._violating_state())
        joined = " ".join(result["error_log"])
        assert REASON_CREDENTIAL in joined
        assert "Traceback" not in joined
        assert "/src/" not in joined

    def test_the_gate_does_not_raise(self):
        """An exception is turned into a bare error partial that clears nothing."""
        OutputFormatNode().execute(self._violating_state())

    def test_the_cleared_set_covers_every_content_bearing_state_field(self):
        """Inventory guard.

        A new output-bearing field must join OUTPUT_BEARING_FIELDS, or the next
        gate violation ships it. Only inert provenance — an enum and a count —
        is allowed to stay behind.
        """
        from framework.schemas.agent_state import AgentState
        from src.schemas.state import State

        allowed_to_stay = {
            # Inert summary of what was accepted: an identifier and two counts.
            "validated_input",
            # The declaration the pipeline ran on, not an answer.
            "runtime_settings",
            # Carries the withheld notice itself.
            "formatted_output",
            # Provenance only: a count and an enum, neither of which is content.
            "anomaly_count",
            "alert_severity",
            # Framework-managed correlation identifiers.
            "trace_id",
            "correlation_id",
            # A closed-set reason code, never caller content. It must NOT join
            # the cleared inventory: clearing it on a withheld output would erase
            # the reason and leave the caller a notice with nothing behind it.
            "error_code",
        }
        declared = set(State.__annotations__) - set(AgentState.__annotations__)
        assert declared, "the template declares no state fields — the guard would be vacuous"
        assert declared - allowed_to_stay <= set(OUTPUT_BEARING_FIELDS)

    def test_a_credential_the_platform_catches_is_caught_here_first(self):
        """A local pattern set narrower than the platform's is a bypass.

        The value would pass this gate, the platform would raise later inside its
        own post-processing, and the partial result that surfaces then is not the
        one this node withheld.
        """
        from framework.security.credential_detector import detect_credentials

        for value in (
            "AKIAIOSFODNN7EXAMPLE",
            "sk_live_" + "abcdefghijklmnop123456",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "eyJhbGciOiJIUzI1NiJ9.payload",
            "Bearer abcdefghij0123456789",
            "redis://cache-host-01.internal:6379/0",
        ):
            assert detect_credentials(value), value
            alert = dict(ALERT)
            alert["summary"] = f"Investigate {value} on the engagement."
            result = OutputFormatNode().execute(_state(management_alert=alert))
            assert result["status"] == AgentStatus.ERROR.value, value


class TestCleanPath:
    """A refuse-everything gate must not be able to pass the containment tests."""

    def test_the_ordinary_report_is_still_produced(self):
        result = OutputFormatNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "ENG-2026-001" in result["formatted_output"]
        assert "revenue_underperform" in result["formatted_output"]

    def test_unanalysed_metrics_are_stated_not_implied(self):
        alert = dict(ALERT)
        alert["unanalysed_metrics"] = ["custom_metric"]
        result = OutputFormatNode().execute(_state(management_alert=alert))
        assert "custom_metric" in result["formatted_output"]
        assert "no baseline available" in result["formatted_output"]

    def test_a_missing_alert_still_reports_a_result(self):
        result = OutputFormatNode().execute(_state(management_alert=None))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]

    def test_trust_level_is_anonymous(self):
        from framework.schemas.invocation_context import TrustLevel

        assert OutputFormatNode.required_trust_level == TrustLevel.ANONYMOUS

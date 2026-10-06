# Unit tests for the domain nodes and the graph composition.
#
# Audit emission is patched per node module rather than through a sys.modules stub,
# which would break the real `shared` package the framework loads at import time:
#   monkeypatch.setattr("src.nodes.<module>.emit_trace_event", lambda *a, **k: None)

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel

PAYLOAD = {
    "engagement_id": "ENG-2026-001",
    "billing_period": "2026-Q2",
    "metrics": {
        "billed_hours": 5200.0,
        "utilization_rate": 0.94,
        "revenue_jpy": 52000000.0,
        "sla_breach_count": 4.0,
        "expense_ratio": 0.38,
    },
    "baseline_metrics": None,
    "team_size": 8.0,
}

SETTINGS = {
    "deviation_threshold_pct": 15.0,
    "severity_high_pct": 30.0,
    "severity_critical_pct": 50.0,
    "target_utilization_rate": 0.75,
    "expected_sla_breach_count": 0.5,
    "expense_ratio_benchmark": 0.28,
    "standard_billable_hours_per_person": 480.0,
    "standard_rate_jpy_per_hour": 12000.0,
    "max_metrics": 32,
    "max_identifier_length": 32,
}


def _base_state(**kwargs):
    state = {
        "user_input": "",
        "input_context": {},
        "validated_input": None,
        "caller_payload": None,
        "enriched_context": None,
        "runtime_settings": dict(SETTINGS),
        "operations_data": None,
        "baseline_data": None,
        "baseline_sources": None,
        "unbaselined_metrics": None,
        "deviations": None,
        "anomaly_classifications": None,
        "anomaly_count": None,
        "alert_severity": None,
        "management_alert": None,
        "formatted_output": None,
        "status": None,
        "error_log": [],
        "node_history": [],
        "session_id": "test-session",
        "correlation_id": "test-corr",
    }
    state.update(kwargs)
    return state


# ── LoadOperationsBaselineNode ────────────────────────────────────────────────


class TestLoadOperationsBaselineNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.load_operations_baseline_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.load_operations_baseline_node import LoadOperationsBaselineNode

        self.node = LoadOperationsBaselineNode()

    def test_publishes_payload_and_baselines(self):
        result = self.node.execute(_base_state(caller_payload=dict(PAYLOAD)))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["operations_data"]["engagement_id"] == "ENG-2026-001"
        assert set(result["baseline_data"]) >= {
            "billed_hours",
            "utilization_rate",
            "revenue_jpy",
            "sla_breach_count",
            "expense_ratio",
        }

    def test_a_baseline_is_never_a_multiple_of_the_value_under_test(self):
        """The defect this node exists to avoid.

        baseline = current x factor makes the deviation a constant of the factor —
        the same percentage for every engagement and every figure ever submitted,
        so the report says the same thing whatever the data says.
        """
        low = self.node.execute(
            _base_state(caller_payload={**PAYLOAD, "metrics": {**PAYLOAD["metrics"], "billed_hours": 100.0}})
        )["baseline_data"]["billed_hours"]
        high = self.node.execute(
            _base_state(caller_payload={**PAYLOAD, "metrics": {**PAYLOAD["metrics"], "billed_hours": 90000.0}})
        )["baseline_data"]["billed_hours"]
        assert low == high == 3840.0

    def test_the_declared_benchmark_is_what_is_used(self):
        settings = {**SETTINGS, "standard_billable_hours_per_person": 100.0}
        result = self.node.execute(_base_state(caller_payload=dict(PAYLOAD), runtime_settings=settings))
        assert result["baseline_data"]["billed_hours"] == 800.0

    def test_caller_history_takes_precedence_over_the_benchmark(self):
        payload = {**PAYLOAD, "baseline_metrics": {"billed_hours": 5100.0}}
        result = self.node.execute(_base_state(caller_payload=payload))
        assert result["baseline_data"]["billed_hours"] == 5100.0
        assert result["baseline_sources"]["billed_hours"] == "caller_history"
        assert result["baseline_sources"]["utilization_rate"] == "benchmark"

    def test_a_metric_with_no_baseline_is_reported_not_invented(self):
        payload = {**PAYLOAD, "metrics": {**PAYLOAD["metrics"], "custom_throughput": 42.0}}
        result = self.node.execute(_base_state(caller_payload=payload))
        assert "custom_throughput" not in result["baseline_data"]
        assert result["unbaselined_metrics"] == ["custom_throughput"]

    def test_billed_hours_has_no_baseline_without_a_team_size(self):
        payload = {**PAYLOAD, "team_size": None}
        result = self.node.execute(_base_state(caller_payload=payload))
        assert "billed_hours" in result["unbaselined_metrics"]
        assert "revenue_jpy" in result["unbaselined_metrics"]

    def test_missing_payload_is_an_error(self):
        result = self.node.execute(_base_state())
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        from src.nodes.load_operations_baseline_node import LoadOperationsBaselineNode

        assert LoadOperationsBaselineNode.required_trust_level == TrustLevel.ANONYMOUS


# ── DetectDeviationsNode ──────────────────────────────────────────────────────


class TestDetectDeviationsNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.detect_deviations_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.detect_deviations_node import DetectDeviationsNode

        self.node = DetectDeviationsNode()

    def _state(self, billed_hours=5200.0, **kwargs):
        ops = {
            "engagement_id": "ENG-001",
            "metrics": {"billed_hours": billed_hours, "utilization_rate": 0.94},
        }
        baseline = {"billed_hours": 3840.0, "utilization_rate": 0.75}
        return _base_state(operations_data=ops, baseline_data=baseline, **kwargs)

    def test_deviation_percentage_is_computed(self):
        result = self.node.execute(self._state(billed_hours=5200.0))
        hours = next(d for d in result["deviations"] if d["metric"] == "billed_hours")
        assert abs(hours["deviation_pct"] - 35.42) < 0.05
        assert hours["threshold_exceeded"] is True

    def test_the_deviation_tracks_the_input(self):
        first = self.node.execute(self._state(billed_hours=5200.0))["deviations"][0]["deviation_pct"]
        second = self.node.execute(self._state(billed_hours=9000.0))["deviations"][0]["deviation_pct"]
        assert first != second

    def test_at_baseline_nothing_is_flagged(self):
        ops = {
            "engagement_id": "ENG-001",
            "metrics": {"billed_hours": 3840.0, "utilization_rate": 0.75},
        }
        state = _base_state(operations_data=ops, baseline_data={"billed_hours": 3840.0, "utilization_rate": 0.75})
        result = self.node.execute(state)
        assert result["deviations"]
        assert all(not d["threshold_exceeded"] for d in result["deviations"])

    def test_the_declared_threshold_is_what_is_applied(self):
        loose = self.node.execute(self._state(runtime_settings={**SETTINGS, "deviation_threshold_pct": 90.0}))
        assert all(not d["threshold_exceeded"] for d in loose["deviations"])
        tight = self.node.execute(self._state(runtime_settings={**SETTINGS, "deviation_threshold_pct": 1.0}))
        assert all(d["threshold_exceeded"] for d in tight["deviations"])

    def test_a_non_finite_deviation_is_dropped_not_treated_as_normal(self):
        """A NaN compares False against every threshold, so returning one would
        report the metric as within parameters."""
        ops = {"engagement_id": "ENG-001", "metrics": {"billed_hours": float("nan")}}
        result = self.node.execute(_base_state(operations_data=ops, baseline_data={"billed_hours": 100.0}))
        assert result["deviations"] == []

    def test_a_ratio_that_overflows_to_infinity_is_dropped_too(self):
        """The second guard, and the only input that reaches it.

        Both operands are finite here, so the guard on the inputs cannot see this
        case: the division is what produces the non-finite value.
        """
        ops = {"engagement_id": "ENG-001", "metrics": {"billed_hours": 1e308}}
        result = self.node.execute(_base_state(operations_data=ops, baseline_data={"billed_hours": 1e-308}))
        assert result["deviations"] == []

    def test_a_zero_baseline_is_skipped(self):
        ops = {"engagement_id": "ENG-001", "metrics": {"billed_hours": 10.0}}
        result = self.node.execute(_base_state(operations_data=ops, baseline_data={"billed_hours": 0.0}))
        assert result["deviations"] == []

    def test_missing_operations_data_is_an_error(self):
        result = self.node.execute(_base_state())
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        from src.nodes.detect_deviations_node import DetectDeviationsNode

        assert DetectDeviationsNode.required_trust_level == TrustLevel.ANONYMOUS


# ── ClassifyAnomalyNode ───────────────────────────────────────────────────────


class TestClassifyAnomalyNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.classify_anomaly_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.classify_anomaly_node import ClassifyAnomalyNode

        self.node = ClassifyAnomalyNode()

    def _state(self, deviation_pct=40.0, metric="billed_hours", **kwargs):
        deviations = [
            {
                "metric": metric,
                "current": 5200.0,
                "baseline": 3840.0,
                "deviation_pct": deviation_pct,
                "threshold_exceeded": True,
            }
        ]
        return _base_state(
            deviations=deviations,
            operations_data={"engagement_id": "ENG-001", "metrics": {}},
            **kwargs,
        )

    def test_classifies_billing_overrun(self):
        result = self.node.execute(self._state(40.0, "billed_hours"))
        assert result["anomaly_classifications"][0]["type"] == "billing_overrun"
        assert result["anomaly_classifications"][0]["severity"] == "high"

    @pytest.mark.parametrize("pct,severity", [(55.0, "critical"), (40.0, "high"), (20.0, "medium")])
    def test_severity_bands(self, pct, severity):
        assert self.node.execute(self._state(pct))["anomaly_classifications"][0]["severity"] == severity

    def test_the_declared_bands_are_what_is_applied(self):
        settings = {**SETTINGS, "severity_critical_pct": 20.0, "severity_high_pct": 10.0}
        result = self.node.execute(self._state(25.0, runtime_settings=settings))
        assert result["anomaly_classifications"][0]["severity"] == "critical"

    def test_revenue_underperformance_is_reachable(self):
        """The type was unreachable while every baseline was a fraction of the
        current value: revenue could then only ever deviate upwards."""
        result = self.node.execute(self._state(-40.0, "revenue_jpy"))
        assert result["anomaly_classifications"][0]["type"] == "revenue_underperform"

    def test_monetary_figures_render_as_aggregates(self):
        deviations = [
            {
                "metric": "revenue_jpy",
                "current": 52123456.0,
                "baseline": 76987654.0,
                "deviation_pct": -32.3,
                "threshold_exceeded": True,
            }
        ]
        state = _base_state(deviations=deviations, operations_data={"engagement_id": "ENG-001", "metrics": {}})
        description = self.node.execute(state)["anomaly_classifications"][0]["description"]
        assert "52,123,000" in description
        assert "52,123,456" not in description

    def test_no_flagged_deviation_reports_normal(self):
        state = _base_state(
            deviations=[
                {
                    "metric": "billed_hours",
                    "current": 3900.0,
                    "baseline": 3840.0,
                    "deviation_pct": 1.6,
                    "threshold_exceeded": False,
                }
            ],
            operations_data={"engagement_id": "ENG-001", "metrics": {}},
        )
        result = self.node.execute(state)
        assert result["alert_severity"] == "normal"
        assert result["anomaly_count"] == 0

    def test_worst_severity_drives_the_alert(self):
        deviations = [
            {
                "metric": "billed_hours",
                "current": 5200.0,
                "baseline": 3840.0,
                "deviation_pct": 40.0,
                "threshold_exceeded": True,
            },
            {
                "metric": "sla_breach_count",
                "current": 12.0,
                "baseline": 0.5,
                "deviation_pct": 2300.0,
                "threshold_exceeded": True,
            },
        ]
        state = _base_state(deviations=deviations, operations_data={"engagement_id": "ENG-001", "metrics": {}})
        assert self.node.execute(state)["alert_severity"] == "critical"

    def test_trust_level_anonymous(self):
        from src.nodes.classify_anomaly_node import ClassifyAnomalyNode

        assert ClassifyAnomalyNode.required_trust_level == TrustLevel.ANONYMOUS


# ── GenerateManagementAlertNode ───────────────────────────────────────────────


class TestGenerateManagementAlertNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.generate_management_alert_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.generate_management_alert_node import GenerateManagementAlertNode

        self.node = GenerateManagementAlertNode()

    def _state(self, **kwargs):
        return _base_state(
            anomaly_classifications=[
                {
                    "type": "billing_overrun",
                    "severity": "high",
                    "metric": "billed_hours",
                    "current_value": 5200.0,
                    "baseline_value": 3840.0,
                    "deviation_pct": 35.4,
                    "description": "Billed hours are 35.4% above the 3840-hour baseline.",
                }
            ],
            anomaly_count=1,
            alert_severity="high",
            baseline_sources={"billed_hours": "caller_history"},
            unbaselined_metrics=["custom_throughput"],
            operations_data={
                "engagement_id": "ENG-2026-001",
                "billing_period": "2026-Q2",
                "metrics": {},
            },
            **kwargs,
        )

    def test_alert_carries_the_required_fields(self):
        alert = self.node.execute(self._state())["management_alert"]
        assert alert["engagement_id"] == "ENG-2026-001"
        assert alert["billing_period"] == "2026-Q2"
        assert alert["severity"] == "high"
        assert alert["anomaly_count"] == 1
        assert len(alert["findings"]) == 1
        assert alert["recommended_actions"]

    def test_alert_id_is_derived_from_the_engagement(self):
        alert = self.node.execute(self._state())["management_alert"]
        assert alert["alert_id"].startswith("ALERT-ENG-2026-001-")

    def test_baseline_provenance_travels_to_the_finding(self):
        alert = self.node.execute(self._state())["management_alert"]
        assert alert["findings"][0]["baseline_source"] == "caller_history"

    def test_unanalysed_metrics_are_carried_not_dropped(self):
        """A metric that was never compared reads as a clean result unless it is
        named."""
        alert = self.node.execute(self._state())["management_alert"]
        assert alert["unanalysed_metrics"] == ["custom_throughput"]

    def test_a_clean_engagement_produces_an_empty_finding_list(self):
        state = _base_state(
            anomaly_classifications=[],
            anomaly_count=0,
            alert_severity="normal",
            operations_data={"engagement_id": "ENG-001", "billing_period": "Q1", "metrics": {}},
        )
        alert = self.node.execute(state)["management_alert"]
        assert alert["findings"] == []
        assert alert["anomaly_count"] == 0

    def test_recommended_actions_are_deduplicated(self):
        state = self._state()
        state["anomaly_classifications"] = state["anomaly_classifications"] * 2
        actions = self.node.execute(state)["management_alert"]["recommended_actions"]
        assert len(actions) == len(set(actions))

    def test_trust_level_anonymous(self):
        from src.nodes.generate_management_alert_node import GenerateManagementAlertNode

        assert GenerateManagementAlertNode.required_trust_level == TrustLevel.ANONYMOUS


# ── Graph composition ─────────────────────────────────────────────────────────


class TestGraphComposition:
    def test_every_backbone_slot_is_filled(self):
        from src.graph.graph import AnomalyDetectionGraphNode, BillingAnomalyDetectionAgent
        from src.nodes.input_validate_node import InputValidateNode
        from src.nodes.output_format_node import OutputFormatNode

        agent = BillingAnomalyDetectionAgent()
        agent.compile()
        assert isinstance(agent._nodes["pre_process"], InputValidateNode)
        assert isinstance(agent._nodes["main"], AnomalyDetectionGraphNode)
        assert isinstance(agent._nodes["post_process"], OutputFormatNode)

    def test_the_manifest_entry_point_resolves(self):
        import importlib

        import yaml

        from src.graph.graph import _RUNTIME_CONFIG_PATH

        manifest = yaml.safe_load((_RUNTIME_CONFIG_PATH.parent / "agent.yaml").read_text(encoding="utf-8"))
        module_path, _, class_name = str(manifest["class"]).rpartition(".")
        assert getattr(importlib.import_module(module_path), class_name)

    def test_the_settings_reach_the_main_slot(self):
        from src.graph.graph import BillingAnomalyDetectionAgent

        agent = BillingAnomalyDetectionAgent(config={"detection": {"deviation_threshold_pct": 3.0}})
        agent.compile()
        forwarded = agent._nodes["main"]._parent_config()
        assert forwarded["settings"]["deviation_threshold_pct"] == 3.0

    def test_parent_config_is_not_empty(self):
        """An empty forward means every declared value is dead inside the inner graph."""
        from src.graph.graph import BillingAnomalyDetectionAgent

        agent = BillingAnomalyDetectionAgent()
        agent.compile()
        assert agent._nodes["main"]._parent_config()["settings"]

    def test_the_bridge_carries_the_payload_the_framework_will_not(self):
        """GraphNode forwards only the string extract_input() returns."""
        from src.graph.context_bridge import get_caller_input_context
        from src.graph.graph import AnomalyDetectionGraphNode

        node = AnomalyDetectionGraphNode(settings=dict(SETTINGS))
        node.extract_input(_base_state(caller_payload=dict(PAYLOAD), validated_input="summary"))
        assert get_caller_input_context()["caller_payload"]["engagement_id"] == "ENG-2026-001"
        assert get_caller_input_context()["runtime_settings"]["deviation_threshold_pct"] == 15.0

    def test_the_inner_graph_seeds_itself_from_the_bridge(self):
        from src.graph.context_bridge import set_caller_input_context
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        set_caller_input_context({"caller_payload": dict(PAYLOAD), "runtime_settings": dict(SETTINGS)})
        seeded = DomainWorkflowGraph()._extra_initial_state()
        assert seeded["caller_payload"]["engagement_id"] == "ENG-2026-001"
        assert seeded["runtime_settings"]["deviation_threshold_pct"] == 15.0

    def test_every_key_merge_output_reads_is_emitted_by_the_inner_graph(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph
        from src.graph.graph import AnomalyDetectionGraphNode

        emitted = set(DomainWorkflowGraph().get_output(_base_state()))
        node = AnomalyDetectionGraphNode(settings=dict(SETTINGS))
        merged = node.merge_output(_base_state(), {key: None for key in emitted})
        assert set(merged) <= emitted

    def test_no_node_declares_a_config_argument_the_framework_never_supplies(self):
        """BaseNode.__call__ calls self.execute(state) and nothing else."""
        import importlib
        import inspect
        import pkgutil

        from framework.nodes.base_node import BaseNode

        package = importlib.import_module("src.nodes")
        for _, module_name, _ in pkgutil.walk_packages(package.__path__, prefix="src.nodes."):
            module = importlib.import_module(module_name)
            for attribute in vars(module).values():
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, BaseNode)
                    and attribute.__module__ == module_name
                ):
                    params = list(inspect.signature(attribute.execute).parameters)
                    assert params == ["self", "state"], f"{attribute.__name__}: {params}"

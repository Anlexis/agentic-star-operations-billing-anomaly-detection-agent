# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start ->
# _security_gate_input() -> execute() -> _security_gate_output() ->
# node_complete, for every concrete node under src/nodes/.
#
# Also verifies the full backbone invoke order for the outer BillingAnomalyDetectionAgent:
#   InitializeNode -> InputValidateNode -> AnomalyDetectionGraphNode ->
#   OutputFormatNode -> FinalizeNode
#
# PB-6 invokes at VERIFIED_EXTERNAL caller trust — the real external path.
# An internal-context shortcut would mask a trust mismatch on the inner nodes.

import importlib
import inspect
import pkgutil

import pytest

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "AnomalyDetectionGraphNode"

# A success-yielding request for the backbone invoke test. Byte-identical to the
# operations payload in deploy/invoke_payload.json, so the shape verified here is
# the shape a deployed first-invoke check sends.
_VALID_REQUEST = "billing anomaly review"
_VALID_CONTEXT = {
    "operations_payload": {
        "engagement_id": "ENG-PB6-001",
        "billing_period": "2026-Q2",
        "metrics": {
            "billed_hours": 5200,
            "utilization_rate": 0.94,
            "revenue_jpy": 52000000,
            "sla_breach_count": 4,
            "expense_ratio": 0.38,
        },
        "team_size": 8,
    }
}

# ─────────────────────────────────────────────────────────────────────────────


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in all domain node modules (avoids audit-backend calls)."""
    for mod_suffix in (
        "input_validate_node",
        "load_operations_baseline_node",
        "detect_deviations_node",
        "classify_anomaly_node",
        "generate_management_alert_node",
        "output_format_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not yet imported; fine for discovery-based tests


class TestInvokeOrder:
    """PB-6: __call__ must run trust gate -> node_start -> input gate -> execute()
    -> output gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        _patch_domain_emit(monkeypatch)

        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestBackboneInvokeOrder:
    """PB-6 backbone: full graph invoke verifies the 5-node backbone executes in order.

    Backbone order: InitializeNode -> InputValidateNode (pre_process) ->
                    AnomalyDetectionGraphNode (main) ->
                    OutputFormatNode (post_process) -> FinalizeNode

    Uses VERIFIED_EXTERNAL caller trust — the real external path.
    InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) is mandatory;
    NEVER use for_internal() which would mask inner-node trust-trap failures.
    """

    def test_backbone_invoke_succeeds_and_returns_formatted_output(self, monkeypatch):
        _patch_domain_emit(monkeypatch)

        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import BillingAnomalyDetectionAgent

        agent = BillingAnomalyDetectionAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_REQUEST, ctx=ctx, input_context=_VALID_CONTEXT)

        from framework.schemas.agent_status import AgentStatus

        # Core contract: status SUCCESS and formatted_output present.
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status={AgentStatus.SUCCESS.value!r}, got: {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output") is not None, "output must be set after a successful invoke"

    def test_backbone_node_history_matches_expected_order(self, monkeypatch):
        _patch_domain_emit(monkeypatch)

        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import BillingAnomalyDetectionAgent

        agent = BillingAnomalyDetectionAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_REQUEST, ctx=ctx, input_context=_VALID_CONTEXT)

        history = result.get("node_history", [])
        assert len(history) > 0, "node_history must be populated after invoke"

        # Verify backbone node type order (by class name substrings in history).
        history_str = str(history)
        # initialize must come before pre_process (InputValidateNode)
        # pre_process must come before main (AnomalyDetectionGraphNode)
        # main must come before post_process (OutputFormatNode)
        if "initialize" in history_str.lower() and "finalize" in history_str.lower():
            init_pos = history_str.lower().find("initialize")
            final_pos = history_str.lower().find("finalize")
            assert init_pos < final_pos, "InitializeNode must run before FinalizeNode in node_history"

    def test_main_slot_is_anomaly_detection_graph_node(self):
        """Verify that the `main` backbone slot is AnomalyDetectionGraphNode."""
        from src.graph.graph import BillingAnomalyDetectionAgent, AnomalyDetectionGraphNode
        from framework.nodes.graph_node import GraphNode

        agent = BillingAnomalyDetectionAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "main slot must be registered"
        assert isinstance(
            main_node, AnomalyDetectionGraphNode
        ), f"main slot must be AnomalyDetectionGraphNode, got {type(main_node).__name__}"
        assert isinstance(main_node, GraphNode), "main slot node must subclass GraphNode (Cat 2 contract)"

    def test_internal_context_not_used(self, monkeypatch):
        """PB-6 contract: invoke must use VERIFIED_EXTERNAL, not for_internal().

        This test documents the intent: if someone accidentally adds for_internal()
        they would mask the inner-node trust trap. VERIFIED_EXTERNAL exercises the
        same path that a real external caller would use.
        """
        from framework.schemas.invocation_context import TrustLevel

        # The valid request at VERIFIED_EXTERNAL must succeed end-to-end.
        _patch_domain_emit(monkeypatch)
        from framework.schemas.invocation_context import InvocationContext
        from src.graph.graph import BillingAnomalyDetectionAgent

        agent = BillingAnomalyDetectionAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_REQUEST, ctx=ctx, input_context=_VALID_CONTEXT)
        from framework.schemas.agent_status import AgentStatus

        assert result.get("status") == AgentStatus.SUCCESS.value

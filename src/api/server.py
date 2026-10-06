"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the platform gateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph, _runtime_config
from src.schemas.caller_contract import screen_credentials

app = FastAPI(title="BillingAnomalyDetectionAgent")

# The registry loads config/config.yaml and passes it as Graph(config=...); the
# standalone server mirrors that exactly, so the declared runtime parameters
# (max_retry, the detection thresholds, the benchmark baselines and the caller
# contract bounds) are live in both deployments rather than only in the one the
# registry starts.
agent = Graph(config=_runtime_config())
agent.compile()
# Namespace and agent name match the manifest values in config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="svc", agent_name="BillingAnomalyDetectionAgent"))

# Upper bound on the serialized structured channel, in bytes. The validation node
# enforces the per-field bounds (identifier length, metric count, numeric ranges);
# this is the coarse adapter guard that stops an oversized request reaching the
# graph at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144


class InvokeRequest(BaseModel):
    input: str = ""
    session_id: str = ""
    # Structured invocation parameters. `operations_payload` carries the billing
    # and operations mapping — engagement label, billing period, metrics, the
    # caller's own historical baselines and team size — each field validated
    # inside the graph (see src/nodes/input_validate_node.py).
    input_context: Optional[Dict[str, Any]] = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the
    # server environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a bearer token and then run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted. This
    # adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so the secrets provider does not apply (no
    # invocation context exists before auth).
    #
    # Required here specifically: InputValidateNode (the pre_process slot)
    # declares required_trust_level = TrustLevel.VERIFIED_EXTERNAL. Nothing else
    # sets request.state.trust_level in a standalone deployment, so without this
    # boundary every request arrives ANONYMOUS, the trust gate denies it, and the
    # agent answers every caller with an empty error envelope.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context = req.input_context or {}
    if input_context:
        # Size is checked on the serialized form, before any field is read: the
        # per-field bounds inside the graph cannot bound a request that is large
        # because of how MANY fields it carries.
        if len(json.dumps(input_context, default=str).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"input_context must be {_MAX_INPUT_CONTEXT_BYTES} bytes or fewer when serialized.",
            )
        # A credential-shaped value anywhere on this channel cannot succeed: the
        # framework's first node returns the structured request verbatim in its
        # own result, and the platform's output gate scans every value of every
        # result, so the run dies at node one with an opaque error the caller
        # cannot act on. Refusing here converts that into an actionable 400 that
        # names the field. 400 rather than 422: pydantic owns 422 and answers it
        # with a list of error objects, so reusing it makes client handling
        # ambiguous.
        offending = screen_credentials(input_context)
        if offending:
            raise HTTPException(
                status_code=400,
                detail=f"{offending} looks like a credential and cannot be accepted.",
            )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "BillingAnomalyDetectionAgent"}

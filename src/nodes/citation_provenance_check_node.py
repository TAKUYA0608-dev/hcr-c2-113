"""HCR-C2-113 — inner workflow step 3 (SoT Step 5): citation_provenance_check.

Deterministically verifies citation completeness for each assessed finding: a *present* finding is cited
only when it has both a document/page anchor AND a verifiable provenance source (``src:<sha8>``, resolved
once at S-1). A present finding lacking either is left uncited (``citation=None``) and routed to
needs-pharmacist-review — enforcing "no claim without a citation". A *missing* finding needs no citation (it
is the evidence gap the register surfaces), but a missing critical field always needs pharmacist review.
Skips (no-op) on rejected / 0-located input, after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BatchRecordEvidenceService
from src.utils.audit import emit_trace_event


class CitationProvenanceCheckNode(FunctionNode):
    """Resolve per-finding citation + pharmacist-review flag (deterministic, fail-closed grounding)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("located_count", 0) == 0:
            emit_trace_event(
                "citation_provenance_check.skip", {"reason": state.get("error_code") or "no_located"}, state
            )
            return {}

        findings = json.loads(state.get("completeness_findings") or "[]")
        checked = [BatchRecordEvidenceService.check_citation(f) for f in findings]

        cited = sum(1 for c in checked if c.get("citation"))
        review_flagged = sum(1 for c in checked if c.get("needs_pharmacist_review"))
        emit_trace_event(
            "citation_provenance_check.complete",
            {"findings": len(checked), "cited": cited, "needs_review": review_flagged},
            state,
        )
        return {"provenance_checked": json.dumps(checked, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

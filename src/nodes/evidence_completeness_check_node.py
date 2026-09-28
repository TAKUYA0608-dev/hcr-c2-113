"""HCR-C2-113 — inner workflow step 2 (SoT Step 4): evidence_completeness_check.

For each located schema field, deterministically determines whether the required evidence is present or
missing in the minimised batch record (presence flag only — the raw attestation value is never interpreted),
assigns a deterministic confidence band, and sets a ``missing_data`` flag. In production the SoT reserves a
bounded LLM for interpreting unevenly-worded attestation text; **this template ships a deterministic
presence check (alias mapping + presence flag, no LLM)** and never fabricates a "present" assertion for a
field it did not receive. Skips (no-op) on rejected / 0-located input, after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BatchRecordEvidenceService
from src.utils.audit import emit_trace_event


class EvidenceCompletenessCheckNode(FunctionNode):
    """Assess present/missing + confidence band for each located schema field (deterministic)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("located_count", 0) == 0:
            emit_trace_event(
                "evidence_completeness_check.skip", {"reason": state.get("error_code") or "no_located"}, state
            )
            return {}

        located = json.loads(state.get("located_fields") or "[]")
        findings = [BatchRecordEvidenceService.assess_completeness(item) for item in located]

        status_distribution: dict[str, int] = {}
        for f in findings:
            status_distribution[f["status"]] = status_distribution.get(f["status"], 0) + 1
        emit_trace_event(
            "evidence_completeness_check.complete",
            {"assessed": len(findings), "status_distribution": status_distribution},
            state,
        )
        return {"completeness_findings": json.dumps(findings, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

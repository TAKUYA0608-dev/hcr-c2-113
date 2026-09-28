"""HCR-C2-113 — Agent state (Hospital Sterile Compounding Batch Record Evidence Completeness Validator, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored
as JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests already-completed sterile-compounding batch records (pseudonymous
identifiers + free-text attestation/lot/timing entries), validates each required evidence field of an
approved in-house evidence schema for presence, keeps a document/page provenance citation for every present
field, and produces an **EvidenceCompletenessRegister** deliverable (candidate) — it never asserts sterility,
never releases a preparation, never changes a compounding record, and never makes a clinical decision.
The final decision (gap remediation, sterility, release, administration) is always an authorized human
pharmacist / physician / quality lead; every finding is candidate / needs-pharmacist-review only, and no
present finding without a verifiable citation is presented (fail-closed).

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the batch-record evidence-completeness validation workflow."""

    # ── pre_process (BatchRecordIntake + SensitiveDataDetectAndMinimise; S-1 + S-2 pre-LLM) ──
    validated_input: str  # JSON: {records[], scope} (patient/staff PII dropped, secrets redacted)
    input_format: str  # "json" | "text" | "empty" | "rejected"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (schema_field_locate → evidence_completeness_check → citation_provenance_check → exception_register_compose) ──
    located_fields: str  # JSON: [{item_id, record_ref, field_key, field_label, category, required, critical, doc_page, has_value, source, clause}]
    located_count: int  # required schema fields located across records (0 → out-of-scope safe answer)
    completeness_findings: str  # JSON: [{..., status(present|missing), missing_data, confidence}]
    provenance_checked: str  # JSON: [{..., citation, needs_pharmacist_review}]
    result: str  # JSON: assembled EvidenceCompletenessRegister (incl. review flags)
    review_required: bool  # True once any finding needs a pharmacist review
    review_status: str  # "needs_pharmacist_review" | "not_required"

    # ── post_process (OutputSanitise — S-3 gate + S-4 audit) ──────────────────
    formatted_output: str  # JSON: final response envelope (register + disclaimer)
    disclaimer: str  # mandatory DRAFT / candidate / needs-pharmacist-review disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ───
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NO_SCHEMA_FIELDS | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail

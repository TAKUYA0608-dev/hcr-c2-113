# Template Design Specification — HCR-C2-113

Hospital Sterile Compounding Batch Record Evidence Completeness Validator (Cat 2).

## Position in AgentCore Architecture

- **Agent Class**: `HospitalSterileCompoundingBatchRecordEvidenceCompletenessAgent` (module-level alias for `Graph`)
- **L1 Base**: **AgentBaseGraph** (L1 direct — Level 2 base agents are deprecated; DocGenerationAgent is a
  pattern reference only). Cat 2 = **GraphNode-in-main**: the outer `AgentBaseGraph` keeps the fixed 5-slot
  backbone and the domain workflow is encapsulated in a `GraphNode` in the `main` slot, wrapping the inner
  `BatchRecordEvidenceValidateWorkflow` (a `BaseGraph`).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — config-free)
  - Graph: composition (`register_nodes()` for node substitution; inner graph via `GraphNode.get_subgraph()`)

## Architecture Overview

### Node Configuration (outer backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level | user_input | (framework) | InitializeNode (default) |
| pre_process | BatchRecordIntake + SensitiveDataDetectAndMinimise (S-1 + S-2 pre-LLM) | user_input | validated_input, input_format, enriched_context, error_code? | `PreProcessNode(FunctionNode)` |
| main | Cat 2 GraphNode wrapping the inner evidence-validation workflow | validated_input | result, located_count, review_required, status | `BatchRecordEvidenceValidateWorkflowGraphNode(GraphNode)` |
| post_process | OutputSanitise (S-3 fail-closed citation + redaction + disclaimer, S-4 audit) | result | formatted_output, disclaimer, audit_logged, error_code? | `PostProcessNode(FunctionNode)` |
| finalize | response_metadata / total_time_ms | formatted_output | (framework) | FinalizeNode (default) |

### Inner workflow (main slot — `src/graph/domain_workflow_graph.py`)

Linear topology with **per-node skip guards** (conditional edges do not propagate across the subgraph
boundary — the portable Cat 2 form):

```
START → schema_field_locate → evidence_completeness_check → citation_provenance_check → exception_register_compose → END
```

| Inner node | Responsibility (SoT step) | Skip guard |
|------|---------------|------------|
| `schema_field_locate` | Step 3 — locate every required seeded-schema field per record; anchor to the record's evidence + resolved provenance; set `located_count` | 0 records / error_code → `located_count=0` + `NO_SCHEMA_FIELDS` |
| `evidence_completeness_check` | Step 4 — present/missing + deterministic confidence band per located field (bounded mapping, **no LLM**) | `error_code` or `located_count==0` → no-op (+ skip S-4 emit) |
| `citation_provenance_check` | Step 5 — fail-closed citation per present finding; uncited/low-conf/critical-gap → `needs_pharmacist_review` | `error_code` or `located_count==0` → no-op (+ skip S-4 emit) |
| `exception_register_compose` | Step 6 — compose the `EvidenceCompletenessRegister` deliverable + citations (or out-of-scope safe answer on 0/error) | 0-findings / error_code → out-of-scope safe answer (`citations=[]`) |

- **Subgraph cache**: `BatchRecordEvidenceValidateWorkflowGraphNode._subgraph` is a **class-level** cache
  (`ClassVar`), assigned via the class (not `self`) so the node holds no mutable instance state (CoE §9).
- **`merge_output` error_code is OUTER-first**: `state.get("error_code") or sub_result.get("error_code")` —
  a pre-stage rejection (`INJECTION_REJECTED` / `INPUT_TOO_LONG`) must survive to the terminal S-4 audit;
  the inner workflow runs on the discarded body and would otherwise overwrite it with `NO_SCHEMA_FIELDS`.

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry)
                                          pre_process
```

### State Definition

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| validated_input | str (JSON) | `{records[], scope}` — patient/staff PII dropped, secrets redacted | progressive |
| input_format | str | json / text / empty / rejected | progressive |
| enriched_context | str (JSON) | `{source, channel}` read-only caller context | progressive |
| located_fields | str (JSON) | per-(record,field) located items anchored to provenance | progressive |
| located_count | int | required schema fields located (0 → out-of-scope) | progressive |
| completeness_findings | str (JSON) | per-field present/missing + confidence | progressive |
| provenance_checked | str (JSON) | per-field citation + needs_pharmacist_review | progressive |
| result | str (JSON) | assembled EvidenceCompletenessRegister | progressive |
| review_required | bool | any finding needs a pharmacist review | progressive |
| formatted_output | str (JSON) | final response envelope (register + disclaimer) | progressive |
| disclaimer | str | mandatory DRAFT / needs-pharmacist-review disclaimer | progressive |
| audit_logged | bool | terminal S-4 audit emitted | progressive |
| error_code | str | INPUT_REJECTED / INJECTION_REJECTED / INPUT_TOO_LONG / NO_SCHEMA_FIELDS / CITATION_INCOMPLETE | degraded only |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable strings for complex fields)
- No JWT, API keys, credentials, patient/staff PII in State (checkpoint DB leakage) — S-1 drops/minimises them
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

## Security model (S-1 … S-5)

- **S-1 (input hygiene, pre_process)**: NFKC + control-char strip + length cap. The record is projected to a
  strict allowlist `{record_id, source, evidence[]}` — every other caller field (patient name / MRN /
  diagnosis / DOB / product / note …) is **dropped** (whitelist-by-construction). `scope` free text is
  hygiened (credential / My-Number / email / phone).
- **S-2 (pre-LLM SensitiveDataDetectAndMinimise + injection containment, pre_process)**: patient/staff PII is
  minimised **before any downstream processing**; the raw attestation *value* is reduced to a presence flag
  (never carried). Injection markers / oversize are surfaced as a **degraded `SUCCESS + error_code`
  (`INJECTION_REJECTED` / `INPUT_TOO_LONG`)** — the S-2 hook **MUST NOT raise and MUST NOT set
  `status=ERROR`** (that would short-circuit past post_process); `execute()` discards the offending body so
  post_process still delivers the safe answer + disclaimer + S-4 audit. No source text is ever interpreted
  semantically (and this template runs **no LLM at all**).
- **Identifier tokenization is UNCONDITIONAL (no syntactic passthrough)**: `record_id` is tokenized to an
  opaque, non-reversible surrogate `rec:<sha8>` — a bare name (with or without spaces) is opaque like any
  value, and a caller value merely shaped like a surrogate (`rec:deadbeef`) is re-hashed, never trusted.
- **Provenance is resolved exactly once at S-1**: a caller `source` becomes a grounded citation only when it
  names an authorized clinical document system of record (`ebr:` / `emr_pharmacy:` / `qms:` / `dms:` …),
  tokenized to `src:<sha8>`. There is **no format-based passthrough** — a forged surrogate `src:1a2b3c4d`
  has an unauthorized namespace → `None` → fail-closed. Downstream trusts the S-1 resolution verbatim and
  never re-resolves.
- **S-3 (OutputSanitise, post_process)**: **per-entry fail-closed citation** — every *present* register
  entry must carry both its own local citation AND an exact top-level `{item_id, source}` citation for the
  same item; a partially ungrounded present finding degrades the whole register to a safe `needs_review`
  answer with the body withheld (`CITATION_INCOMPLETE`, still `SUCCESS`). *Missing* findings are the evidence
  gaps the register surfaces and need no citation. Re-redact secret / contact / person- or company-name
  patterns (defense-in-depth), and append the mandatory DRAFT / needs-pharmacist-review disclaimer (the
  gate raises if it is missing).
- **S-4 (no-persist audit)**: every `execute()` emits a domain event (counts / status + category
  distribution / review flag / error_code only — never a patient name, patient ID, diagnosis, attestation
  value, or staff name); the raw record is not retained.
- **S-5**: `==` exact dependency pinning; framework-injected rate limiting.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, trust level via ctx)
- [x] S-2: `_extra_security_gate_input()` — size cap + injection markers (degraded SUCCESS, never raises)
- [x] S-3: `_extra_security_gate_output()` — mandatory disclaimer preservation (may raise)
- [x] S-4: `emit_trace_event()` — one domain event on every `execute()` path (incl. skip / degraded / safe)

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass (pre_process / post_process / all 4 inner nodes) → framework `@final` gate runs
>   automatically; extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only
> - `GraphNode` (`main` slot) → deliberate no-op (the inner FunctionNodes' gates already apply)

### Composition Pattern
- **Pattern**: GraphNode (subgraph) in the `main` slot wrapping an inner `BaseGraph`
- **Composition target**: `BatchRecordEvidenceValidateWorkflow` (inner 4-node linear workflow)
- **Error propagation strategy**: propagate (degraded findings surface as SUCCESS + error_code)

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (via `framework.*`; agent-local via `src.` prefix)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Deterministic multi-step workflow, no autonomous loop |
| Composition pattern | FunctionNode-in-main (Cat 1) | GraphNode-in-main (Cat 2) | **GraphNode-in-main** | HCR job-to-be-done orchestrating locate → check → cite → compose |
| Human review | separate HumanGate node | per-finding `needs_pharmacist_review` flag + disclaimer | **review flags + disclaimer** | Output is a diagnostic register (no action to gate); every finding is candidate, all decisions defer to a human pharmacist |
| LLM usage | bounded LLM for attestation mapping | deterministic alias mapping | **deterministic (no LLM)** | Reproducible + auditable; no model in config, no LLM dependency |

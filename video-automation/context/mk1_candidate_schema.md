# MK1 canonical candidate schema

**Scope:** MK1 selection pipeline only. Legacy selection (`select_clip.py`,
`selection.json`, HH:MM:SS segments) is a separate path and is not covered
here.

**Upgrade route:** [MK1-first formalisation](../../system-context/selection-upgrade/architecture-guardrails.md).

**Code source of truth:** `video-automation/scripts/processing_contracts.py`
(`CANONICAL_MK1_CANDIDATE_*` constants, `validate_mk1_candidate`).

---

## 1. What a candidate is

A **candidate** is a possible clip moment — not a rendered clip and not an
approved final output.

```text
Discovery finds candidate moments.
Candidate Processing cleans and dedupes them.
Evaluation chooses which processed candidates become rendered clips.
Rendering cuts evaluated selections only.
```

Timestamps are **float seconds** (`start_sec`, `end_sec`). MK1 does not use
legacy `HH:MM:SS` segment strings.

---

## 2. One canonical shape

There is **one** canonical MK1 candidate object from pool assembly onward:

```text
raw_candidate_pool.json  →  candidates[]
```

Schema identifiers:

| Identifier | Value | Meaning |
|------------|-------|---------|
| Pool envelope | `raw_candidate_pool_v1` | Artifact wrapping the candidate list |
| Canonical candidate | `mk1_candidate_v1` | Shape of each `candidates[]` entry |

The canonical candidate is **not** a third competing object. It is the existing
`raw_candidate_pool_v1` candidate entry formalised under the name
`mk1_candidate_v1`.

**Pre-pool discovery objects** use `candidate_local_id` inside
`section_candidate_discovery.json`. Pool assembly assigns deterministic
`candidate_id` via `make_candidate_id()` in `processing_integration.py`.

---

## 3. Canonical candidate object

```json
{
  "candidate_id": "cand_…",
  "source_section_id": "section_0001",
  "start_sec": 10.0,
  "end_sec": 55.0,
  "duration_sec": 45.0,
  "hook_text": "…",
  "core_idea_summary": "…",
  "why_candidate_has_potential": "…",
  "archetype": "valuable_insight",
  "confidence": 0.82,
  "scores": {
    "hook_strength": 8,
    "standalone_context": 7,
    "insight_value": 9,
    "retention_potential": 8,
    "natural_ending": 7,
    "overall_potential": 8
  },
  "warnings": [],
  "transcript_quality_flags": []
}
```

---

## 4. Field classifications

| Field | Classification | Notes |
|-------|----------------|-------|
| `candidate_id` | **Required** | Assigned at pool assembly; deterministic for same job/section/timestamps |
| `source_section_id` | **Required** | Links to `transcript_sections.json` / `section_candidate_discovery.json` |
| `start_sec` | **Required** / **Render-required** | Numeric seconds ≥ 0 |
| `end_sec` | **Required** / **Render-required** | Numeric; must be > `start_sec` |
| `duration_sec` | **Required** / **Derived** | Must match `end_sec - start_sec` within 0.001s |
| `hook_text` | **Required** | Evidence text; may be empty string if discovery defaulted |
| `core_idea_summary` | **Required** | Evidence text |
| `why_candidate_has_potential` | **Required** | Evidence text |
| `archetype` | **Required** | Enum; see `ALLOWED_CANDIDATE_ARCHETYPES` |
| `confidence` | **Required** | 0.0–1.0; used by Evaluation thresholds |
| `scores` | **Required** | Object with six 0–10 components; see below |
| `warnings` | **Required** | String list; diagnostic metadata (may be `[]`) |
| `transcript_quality_flags` | **Required** | Enum list; diagnostic metadata (may be `[]`) |

### Score components (`scores`)

All **required** on the canonical candidate. Each value must be numeric **0–10**.

| Field | Classification | Used by |
|-------|----------------|---------|
| `hook_strength` | Required attribute | Evaluation ranking |
| `standalone_context` | Required attribute | Evaluation ranking |
| `insight_value` | Required attribute | Evaluation ranking |
| `retention_potential` | Required attribute | Evaluation ranking |
| `natural_ending` | Required attribute | Evaluation ranking |
| `overall_potential` | Required attribute | Evaluation thresholds + primary rank key |

**Score guardrail:** These are model-provided candidate attributes. They are
**not** an evidence-based weighted quality model. Do not add composite scores,
fixed weights, or legacy score field mappings (`clarity_standalone`, `overall`,
etc.) to the canonical candidate.

---

## 5. Stage ownership

### Transcript Presentation strategy

MK1 transcript presentation (`transcript_sectioning.py`) uses deterministic
**fixed partitions** with **fixed overlap** and **Whisper segment-boundary
snapping**. Defaults (300s target, **60s overlap**) are tunable via
`processing_settings.resolve_sectioning_config()` — not proven optima. Ops UI
saved values and env vars override these defaults when set.
`transcript_sections.json` records strategy metadata under `presentation`.

### Discovery strategy

MK1 Discovery (`section_candidate_discovery.py`, ai-service) is **recall-oriented
candidate discovery**. It identifies transcript moments with genuine short-form
potential within each section independently. It does not make final render
decisions, rank globally, or act as Evaluation. Default per-section cap is **5**
(safety bound, not a selection limit; tunable, not benchmarked). Ops UI saved
values and env vars override when set. `section_candidate_discovery.json` records
strategy metadata under `discovery`.

### Candidate Processing strategy

MK1 Candidate Processing (`candidate_processing.py`) runs deterministic cleanup
after Discovery: boundary sanity, timestamp overlap/dedupe, and rejection
metadata. It does not call AI, rank final clips, or decide what gets rendered.
Artifact: `candidate_processing.json` (`mk1_candidate_processing_v1`).
Pool assembly reads processed candidates, not raw Discovery output.

### Evaluation strategy

MK1 Evaluation (`selection_gate_v1.py`) is the **only** stage that decides which
processed candidates become rendered clips. Input: canonical candidates from
`raw_candidate_pool.json` (post Candidate Processing). Output:
`selection_result.json` with selected/reserve/rejected **entries** — not
canonical candidates. Strategy: `mk1_selection_gate_evaluation_v1`.

Evaluation uses deterministic thresholds and ranking over Discovery-provided
score fields. This is an operational baseline, not statistically validated
performance scoring. No AI Evaluation yet.

**Execution location today:** Evaluation runs at the start of MK1 post-processing
(`post_processing_mk1.py`), before rendering — not as a post-render stage.

| Stage | Module | Candidate contract role |
|-------|--------|------------------------|
| Transcript Presentation | `transcript_sectioning.py` | Fixed-partition sections (`mk1_fixed_partition_v1`); no candidate object yet |
| Discovery | `section_candidate_discovery.py`, ai-service | Recall-oriented discovery (`mk1_recall_oriented_discovery_v1`); pre-pool candidates with `candidate_local_id` |
| Candidate Processing | `candidate_processing.py`, boundary/overlap helpers | Validates/dedupes pre-pool candidates (`mk1_candidate_processing_v1`) |
| Candidate Object | `processing_integration.py`, `processing_contracts.py` | Pool assembly assigns `candidate_id`; validates canonical shape |
| Evaluation | `selection_gate_v1.py` | Final clip selection (`mk1_selection_gate_evaluation_v1`); selected/reserve/rejected entries |
| Rendering | `render_clip_v1.py` | Reads `candidate_id`, `start_sec`, `end_sec` from selected entry |

---

## 6. Raw candidate vs evaluated entry

### Canonical candidate (raw)

The object in `raw_candidate_pool.json` → `candidates[]`.

Validated by `validate_mk1_candidate()` / `validate_raw_candidate_pool()`.

### Evaluated / selected entry

Produced by `selection_gate_v1.py` in `selection_result.json`:

```text
selected_candidates[]
reserve_candidates[]
rejected_candidates[]
```

A **selected entry** wraps the canonical candidate plus Evaluation metadata:

| Field | Classification |
|-------|----------------|
| `candidate_id`, `start_sec`, `end_sec`, … | Copied from canonical candidate |
| `rank` | **Evaluation-only** |
| `selection_reason` / `reserve_reason` / `rejection_reasons` | **Evaluation-only** |
| `source_candidate` | **Evaluation-only** — full canonical candidate snapshot |

Do not replace the canonical candidate with the selected entry shape. The
conveyor passes the selected entry to `render_clip_v1`, which reads render-required
fields from it (and may use `source_candidate` for metadata).

---

## 7. Timing contract

```text
start_sec   — finite number, not bool
end_sec     — finite number, not bool, end_sec > start_sec
duration_sec — finite number, abs(duration_sec - (end_sec - start_sec)) <= 0.001
```

MK1 remains float-second based. Do not convert to legacy `HH:MM:SS`.

---

## 8. Candidate identity contract

```text
candidate_id = make_candidate_id(job_id, source_section_id, start_sec, end_sec[, index])
```

- Required after pool assembly
- Stable/deterministic for the same inputs (SHA-256 digest, `cand_` prefix)
- `source_section_id` links back to transcript sections and discovery artifacts

Do not change the ID algorithm without an explicit migration decision.

---

## 9. Warnings and transcript quality flags

**Purpose:** Diagnostic / processing metadata only.

- `warnings` — free-form strings (e.g. boundary warnings from Candidate Processing)
- `transcript_quality_flags` — closed enum set (`ALLOWED_TRANSCRIPT_QUALITY_FLAG_VALUES`)

Evaluation **may** block on configured flag/warning sets (`selection_gate_v1`
modes). They must not be turned into weighted scoring inputs.

---

## 10. Validation

```python
from processing_contracts import validate_mk1_candidate, validate_raw_candidate_pool
```

- `validate_mk1_candidate(candidate)` — single candidate
- `validate_raw_candidate_pool(pool)` — full pool including all candidates

Validation is deterministic: field presence, types, ranges, duration consistency,
archetype/flag enums. No AI, no Evaluation, no boundary adjustment.

The MK1 staged artifact chain including `mk1_candidate_v1` pool validation is
covered by `tests/test_mk1_selection_pipeline_smoke.py` (deterministic smoke test,
no real AI or rendering).

---

## 11. Legacy non-goal

This schema does **not** apply to:

- Legacy `select_clip.py` clip/segment objects
- ai-service `clip_selection` `clip_candidates_v2` candidates
- Legacy `selection.json` HH:MM:SS segments

Legacy/MK1 unification and compatibility shims are out of scope until an
explicit migration prompt.

---

## 12. Unresolved decisions (do not invent silently)

- Final field additions for future Candidate Processing enrichments
- Whether Evaluation may adjust candidate boundaries
- Final clip count policy across modes
- When/how legacy consumes MK1 outputs

Future prompts should expose these decisions rather than silently extending the schema.

---

## 13. MK1 AI prompt boundaries

| Stage | AI prompt | Contract |
|-------|-----------|----------|
| Discovery | `section_candidate_discovery_base_v1.txt` | Recall-oriented candidate discovery only; transcript-only; not final selection |
| Evaluation | **None** | Deterministic `selection_gate_v1` (`mk1_selection_gate_evaluation_v1`) |

There is currently **no MK1 AI Evaluation prompt**. A future AI Evaluation stage
would require a separate explicit prompt and design step.

Legacy `ai-service/prompts/clip_selection_v2.txt` is out of scope for MK1 staged
selection formalisation.

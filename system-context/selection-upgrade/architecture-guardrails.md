# MK1 Selection Architecture Guardrails

Canonical guardrails for the MK1-first selection upgrade. Later implementation
prompts should follow this document. It does not change runtime behaviour; it
records the agreed migration route and boundaries.

Related planning context: [rough-plan.md](./rough-plan.md).

**Canonical MK1 candidate schema:**
[`video-automation/context/mk1_candidate_schema.md`](../../video-automation/context/mk1_candidate_schema.md).

---

## 1. Selected route

**The selection upgrade is MK1-first formalisation.**

MK1 already contains the closest version of the target staged architecture in
this repository. The upgrade should formalise that path into explicit stages
before attempting legacy migration.

Implications:

- Formalise the existing MK1 path into the new staged architecture.
- Preserve legacy selection behaviour for now.
- Do not unify legacy and MK1 in the same implementation pass.
- Do not build legacy ↔ MK1 compatibility shims yet.
- Do not create a third selection architecture beside legacy and MK1.
- Do not switch the default pipeline mode until MK1 is proven.

Legacy/MK1 unification and deprecation are **later, explicit migration tasks** —
not implicit goals of early formalisation prompts.

---

## 2. Current no-touch areas (first implementation phase)

The first implementation phase should **not** modify:

| Area | Notes |
|------|-------|
| Legacy `video-automation/scripts/select_clip.py` | Default-path selector subprocess |
| Legacy `video-automation/scripts/clip_video.py` | Legacy FFmpeg render handoff |
| Legacy `video-automation/scripts/pipeline_utils.postprocess_segments` | Legacy filter/rank/dedupe |
| Legacy `ai-service/tasks/clip_selection.py` | Legacy ai_service discovery task |
| Legacy `ai-service/transcript_chunking.py` | Legacy ai_service partitioner |
| Uploading | Out of selection scope |
| Scheduling | Out of selection scope |
| Operations UI | Out of selection scope unless selection output visibility already depends on it |
| Post-render post-processing modules | Captions, reframing, platform format, etc. run after render |
| Default pipeline mode | Must remain `legacy` until an explicit migration prompt changes it |

These may be revisited later through an explicit **legacy deprecation /
unification** prompt.

---

## 3. Target MK1 stage responsibilities

```text
Transcript Presentation:
  Prepare transcript sections for Discovery.

Discovery:
  Find candidate moments. Do not make final render decisions.

Candidate Object:
  The canonical unit that flows through the MK1 selection pipeline.

Candidate Processing:
  Apply candidate-level processing such as boundary sanity, overlap control,
  dedupe, and future enrichments.

Evaluation:
  Choose which processed candidates become rendered clips.

Rendering:
  Render evaluated candidates. Rendering should not rediscover or re-evaluate
  candidates.

Post Processing:
  Improve already-rendered clips.
```

**Terminology guardrails:**

- **Candidates are not finished clips.** A candidate is an inspectable moment
  worth evaluating; a clip is a rendered media file.
- **Discovery must not make final render decisions.** Discovery is recall-oriented
  scouting within a bounded transcript section.
- **Evaluation** (`selection_gate_v1` baseline) decides which processed
  candidates proceed to rendering. Artifact: `post_processing/selection/selection_result.json`.
  Executed at the start of MK1 post-processing today (`post_processing_mk1.py`).

---

## 4. Existing MK1 baseline mapping

Current baseline only — not claimed as final architecture.

| Target stage | Current baseline |
|--------------|------------------|
| Transcript Presentation | `video-automation/scripts/transcript_sectioning.py` |
| Discovery | `video-automation/scripts/section_candidate_discovery.py`; `ai-service/tasks/section_candidate_discovery.py` (`mk1_recall_oriented_discovery_v1`) |
| Candidate Object | `raw_candidate_pool.json`; `video-automation/scripts/processing_contracts.py`; `video-automation/scripts/processing_integration.py` |
| Candidate Processing | `candidate_processing.py`; `candidate_boundary_sanity.py`; `candidate_overlap_control.py` |
| Evaluation | `selection_gate_v1.py` (`mk1_selection_gate_evaluation_v1`); executed at start of `post_processing_mk1.py` |
| Rendering | `video-automation/scripts/post_processing_conveyor.py`; `video-automation/scripts/render_clip_v1.py` |

**Legacy path (stable, separate):** `server/app.py` selector windows →
`select_clip.py` → ai-service `clip_selection` or OpenAI → `postprocess_segments`
→ `selection.json` → `clip_video.py`.

---

## 5. Explicit non-goals

This upgrade does **not** currently implement:

- Visual analysis
- Emotion analysis
- Face analysis
- Audio analysis
- Analytics feedback loops
- Statistical learning
- Data-driven scoring
- Legacy deprecation
- Default mode switch
- New upload systems
- New scheduling systems
- New dashboard / UI redesign

These are future possibilities, not part of immediate MK1 selection
formalisation.

---

## 6. Unresolved decisions handling

Some design choices are intentionally unresolved. Implementation prompts must
**not** silently invent answers to:

- Final candidate schema details (beyond current `raw_candidate_pool_v1` baseline)
- Transcript partition size
- Overlap duration
- Evaluation behaviour beyond the current `selection_gate_v1` baseline
- Whether Evaluation can adjust candidate boundaries
- How many final clips should be selected
- When legacy should be deprecated
- Whether legacy should eventually consume MK1 outputs

For unresolved areas, future implementation prompts should:

- Choose small reversible defaults only when safe
- Expose architectural decisions clearly
- Report unresolved decisions at the end of the prompt
- Avoid hidden compatibility shims
- Avoid fake mathematical scoring systems

---

## 7. No arbitrary scoring guardrail

**Do not reintroduce arbitrary weighted rubric scoring.**

Evaluation may use:

- Deterministic thresholds
- Ranking
- Accept / reject decisions
- Candidate metadata and warnings

Do **not** invent weighted scoring systems that pretend to be evidence-based
(for example, composite formulas with fixed weights presented as objective
quality measures).

Model-provided rubric scores from Discovery may inform human-readable evidence,
but they must not become a new hidden selection engine with invented weights.

Future data-driven scoring can be considered only after enough real performance
data exists.

---

## 10. MK1 AI prompt boundaries

| Stage | AI prompt? | Notes |
|-------|------------|-------|
| Transcript Presentation | **No** | Deterministic sectioning only |
| Discovery | **Yes** | `ai-service/prompts/section_candidate_discovery_base_v1.txt` (`mk1_recall_oriented_discovery_v1`) — candidate discovery only, not final selection |
| Candidate Processing | **No** | Deterministic boundary/dedupe |
| Evaluation | **No** | `selection_gate_v1` (`mk1_selection_gate_evaluation_v1`) — deterministic thresholds/ranking; **no MK1 AI Evaluation prompt exists today** |

Do not add an AI Evaluation prompt without an explicit future design step.
Legacy `clip_selection_v2.txt` remains separate and must not be conflated with MK1 Discovery.

---

## 8. Debuggability guardrail

Staged pipeline work should keep intermediate outputs inspectable.

Intended inspectable chain:

```text
transcript sections
  → discovered candidates
  → processed candidates
  → evaluated candidates
  → rendered clip decisions
```

Not all stages are fully persisted in production today. Future prompts should
move toward this chain without reducing existing observability.

**Automated coverage:** `video-automation/tests/test_mk1_selection_pipeline_smoke.py`
runs a deterministic, mocked end-to-end smoke test over the MK1 staged artifact
chain (processing artifacts → Evaluation → render handoff contract). No real AI,
GPU, or ffmpeg rendering is required.

---

## 11. MK1 processing config defaults

MK1 unset-config defaults (code, Ops UI field defaults, and `.env.example`):

| Setting | Default | Notes |
|---------|---------|-------|
| `section_overlap_sec` | **60** | Transcript Presentation overlap (Prompt 6) |
| `max_candidates_per_section` | **5** | Discovery safety bound (Prompt 7) |

These are tunable defaults, not benchmarked optima. Resolution order:

```text
Ops UI saved value (controls.json) → environment variable → built-in default
```

Explicit saved controls or env vars override code defaults. Do not silently
migrate operator-saved values; reset or migrate only via an explicit prompt.

---

## 9. Two-path separation guardrail

The repository intentionally maintains **two separate selection paths**:

```text
Legacy (default):
  WhisperX → app.py selector windows → select_clip.py → clip_selection/OpenAI
  → postprocess_segments → selection.json → clip_video.py

MK1 (formalisation target):
  WhisperX → transcript_sectioning → section_candidate_discovery
  → raw_candidate_pool.json → selection_gate_v1 → render_clip_v1 conveyor
```

Do not merge these paths, add shims between them, or route legacy through MK1
modules without an explicit migration prompt.

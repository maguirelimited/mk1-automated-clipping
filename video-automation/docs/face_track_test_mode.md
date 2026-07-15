# Face-track test mode — configuration and dev operating procedure

Production default reframing remains **`blur_background`**. Face-track is opt-in
only. This document is the **canonical operator guide** for face-track test mode:
what it does, how to enable it safely in **dev**, how to inspect results in Ops
UI, and when to continue or pause testing.

For smoke-harness commands on single clips, see
[face_track_reframing_smoke.md](face_track_reframing_smoke.md). For Ops UI page
layout, see [ops-ui/README.md](../../ops-ui/README.md) (Outputs section).

---

## 1. What face-track test mode is

Face-track test mode lets **`auto`** reframe mode use **`face_track_crop`** only
when the existing eligibility gate passes. If eligibility fails, **`auto` falls
back to `blur_background`** — the same blurred letterbox path used in production.

```text
auto + face_track_test_enabled=true
  → eligible solo talking-head  → face_track_crop
  → ineligible (split, b-roll, gaps, etc.)  → blurred_background_fit_foreground

auto + face_track_test_enabled=false  (production-safe)
  → blur only; face pipeline not run

reframe_mode=blur_background  (production default)
  → blur only regardless of test flag
```

**Expectations for MK1:**

- **Low face-track usage rate is normal.** Organic candidate batches often yield
  ~10–15% face-track; most funnel clips are mixed, split-screen, or B-roll heavy.
- **Conservative fallback is good.** False negatives (blur when face-track might
  work) are acceptable; false positives (bad face-track on unsuitable content)
  are not.
- **Bad face-track renders are the main thing to avoid.** A wrong crop on
  split-screen or B-roll is worse than blur.

Strict **`face_track`** mode (no blur fallback) is for diagnostics and
known-good clips only — not for routine dev batches.

---

## 2. When to use it

**Use dev test mode when:**

- Testing new funnel or source candidates after post-processing completes
- Reviewing talking-head-heavy sources where face-track may improve framing
- Validating another funnel before wider dev rollout (once selection-gate jobs exist)
- Checking whether face-track output quality beats blur on eligible clips

**Do not use it when:**

- Running production posting or upload workflows
- Running unreviewed new source types in **prod**
- Debugging unrelated conveyor failures (captions, selection, validation, etc.)
- Trying to force face-track on split-screen, B-roll, or slide-heavy content
- Changing production defaults to `auto` or enabling test mode in prod

---

## 3. Configuration

### Intended dev test settings

```yaml
format:
  reframe_mode: auto
  face_track_test_enabled: true
```

### Production-safe settings (unchanged)

```yaml
format:
  reframe_mode: blur_background
  face_track_test_enabled: false
```

### Where settings are resolved

| Source | Key / field |
|--------|-------------|
| Ops UI → Settings → Post-processing → **Face-track test mode** | `format_face_track_test_enabled` |
| Environment | `POST_PROCESSING_FACE_TRACK_TEST_ENABLED` |
| Job resolved config | `format.face_track_test_enabled` |
| Reframe mode (same surfaces) | `format_reframe_mode` / `POST_PROCESSING_FORMAT_REFRAME_MODE` / `format.reframe_mode` |

Set **`reframe_mode: auto`** and **`face_track_test_enabled: true`** together in
dev. Test mode has no effect when `reframe_mode` is `blur_background`.

### Behaviour matrix

| `reframe_mode` | `face_track_test_enabled` | Result |
|----------------|---------------------------|--------|
| `blur_background` | any | Blur only; face pipeline not run |
| `auto` | `false` (default) | Blur only; `face_track_skip_reason=face_track_test_disabled` |
| `auto` | `true` | Face pipeline runs; eligible → face-track crop; ineligible → blur fallback |
| `face_track` | any | Strict mode; eligible required; no blur fallback on failure |

Clip metadata fields to verify: `face_track_test_enabled`,
`face_track_attempted`, `face_track_used`, `face_track_eligibility_reason`,
`face_track_skip_reason`, `format_strategy`.

---

## 4. Pre-run safety checklist

Before running a dev batch or pipeline with test mode enabled, confirm:

```text
[ ] Environment is dev (not prod)
[ ] Upload/posting is disabled or not part of this test
[ ] reframe_mode is auto (for test batches)
[ ] face_track_test_enabled is true only in dev/test config
[ ] Production config still has blur_background + face_track_test_enabled=false
[ ] Sufficient disk space for job outputs and any /tmp batch dirs
[ ] Operator knows where to inspect results (/ops/outputs, job detail)
```

If any prod setting was changed, revert before continuing.

---

## 5. Batch size guidance

| Batch type | Size | Purpose |
|------------|------|---------|
| Small smoke | 5–10 clips | Quick gate check after config or code changes |
| Organic dev batch | 20–30 clips | Real selection-gate candidates; primary rollout evidence |
| Multi-funnel validation | Per funnel, 20–30+ total | Only after another funnel has **real selection-gate jobs** |

Prefer **organically selected candidates** from `selection_result.json`, not
hand-cut perfect talking-head windows. Include mixed, split, and B-roll clips —
the gate should reject them.

For ad-hoc single-clip checks, use the smoke harness (see
[face_track_reframing_smoke.md](face_track_reframing_smoke.md)).

---

## 6. Inspecting results in Ops UI

Open the Operations UI via SSH tunnel (`/ops`). Primary pages:

| Page | Path | What to check |
|------|------|---------------|
| Outputs list | `/ops/outputs` | Per-clip **Face-track / Fallback / Blur / Disabled / Failed / Unknown** badge; reason in subtitle or tooltip |
| Clip detail | `/ops/outputs/<job_id>/<clip_id>` | **Reframing** section: mode, test mode, strategy, eligibility, reason, metrics; **Metadata warnings** only if inconsistent |
| Job Inspector | `/ops/jobs/<job_id>` | Per-clip reframe badge in output list; job-level summary (e.g. `3 face-track, 23 blur fallback`) |

Filter outputs by job: `/ops/outputs?job_id=<job_id>`.

**Note:** Clips processed **before** face-track metadata was added show
**Unknown** — expected for old blur-only jobs. Re-run post-processing in test
mode to populate reframe fields.

---

## 7. Badge and status meanings

| Badge | Meaning |
|-------|---------|
| **Face-track** | Used `face_track_crop`; eligibility passed |
| **Fallback** | `auto` attempted face-track; eligibility failed → blur |
| **Blur** | Production/default blur path (`blur_background` or test off) |
| **Disabled** | `auto` with test mode off; face-track not attempted |
| **Failed** | Strict `face_track` failure or **metadata inconsistency** |
| **Unknown** | Missing `platform_safe_format_v1` reframe metadata (often old jobs) |

Detail page titles follow the same logic, e.g.:

```text
Face-track: Used
Face-track: Fallback — leading_no_face_gap
Face-track: Disabled — face_track_test_disabled
```

**Fallback is normal, not an error.** Ops UI uses a muted badge, not the failure
tone. **Unknown** is expected for pre-metadata pipeline runs.

Common fallback reasons (all normal):

```text
face_track_test_disabled
leading_no_face_gap
insufficient_face_coverage
long_no_face_gap
insufficient_sustained_face_run_pct
```

---

## 8. Acceptable vs not acceptable outcomes

### Acceptable

```text
Clean solo talking-head     → Face-track badge
Mixed / B-roll / split      → Fallback badge
Old jobs without metadata   → Unknown badge
Normal eligibility reject   → Fallback with clear reason
Low face-track rate         → OK (often 10–15% on organic MFM batches)
Overly conservative blur    → OK for MK1 (prefer false negative)
26/26 validation PASS       → Expected on healthy batches
```

### Not acceptable — stop and investigate

```text
Bad face-track crop on unsuitable content (split, B-roll, slides)
Captions covering mouth or chin on face-track outputs
face_track_used=true while face_track_eligible=false
format_strategy=face_track_crop but face_track_used=false
Validation, audio, or duration failures on face-track path
Normal fallback shown as Failed (UI bug)
Metadata warnings on every normal fallback (UI bug)
Production defaults changed to auto or test enabled accidentally
```

---

## 9. Review classification (manual visual QA)

When spot-checking outputs (preview on clip detail or exported MP4):

**Face-track outputs:**

| Term | Meaning |
|------|---------|
| `good_face_track` | Centered face, stable crop, captions clear of mouth |
| `acceptable_face_track` | Minor headroom or drift; still usable |
| `bad_face_track_should_have_fallen_back` | Wrong subject, jumpy crop, or unsuitable layout — **blocker** |
| `unusable_face_track` | Broken render, artifacts, or unreadable — **blocker** |

**Blur fallbacks:**

| Term | Meaning |
|------|---------|
| `correct_fallback` | Blur appropriate for content (split, B-roll, gaps) |
| `overly_conservative_fallback` | Solo clip blurred; gate may be strict — **OK for MK1** |
| `missed_face_track_opportunity` | Clear solo clip blurred; note for future threshold review |
| `incorrect_fallback_reason` | Metadata reason does not match visual content — investigate |

For MK1: **`overly_conservative_fallback` is acceptable**;
**`bad_face_track_should_have_fallen_back` is not**.

Review at minimum: **all face-track outputs**, **all Failed/Metadata warning
clips**, **borderline metrics** (coverage ~65–75%, run_pct ~70–80%), and a
**sample of Fallback clips per source**.

---

## 10. Go / no-go guidance

### Continue dev testing (GO) if:

```text
0 bad face-track renders in the batch
0 metadata inconsistencies (face_track_used ↔ eligibility ↔ format_strategy)
Captions readable on all face-track outputs (no mouth overlap)
Validation passes on all clips in the batch
Fallback reasons match visual content
Ops UI badges match metadata (no systematic mislabeling)
Production defaults unchanged
```

### Pause and investigate (NO-GO) if:

```text
Any bad_face_track_should_have_fallen_back or unusable_face_track
Captions overlap face/mouth on multiple face-track clips
UI shows Metadata warnings on consistent metadata
Validation, audio, or duration regressions on face-track clips
face_track_used and format_strategy disagree in metadata
Prod config accidentally set to auto or test enabled
```

After NO-GO: disable test mode on affected environments, document clip IDs and
artifacts, and file a targeted fix prompt — do not tune thresholds or enable
prod without a separate approved prompt.

---

## 11. Multi-funnel limitation (current)

As of the latest rollout review (**Prompts 17A / 18C**):

```text
Only mfm_business_ai_001 has real selection-gate candidate data in dev.
Six dev jobs exist; five produced 26 organic selected/reserve candidates.
business_clips_test and other funnels have no selection-gate jobs yet.
```

**True multi-funnel validation must wait** until another funnel completes
post-processing with `selection_result.json`. Do not claim multi-funnel
face-track safety until that data exists. When a new funnel is ready, repeat this
procedure (20–30 organic clips, Ops UI review, go/no-go).

---

## 12. Test evidence (operator context)

Concise results from controlled dev batches — not a substitute for reviewing
**your** job in Ops UI.

| Prompt | Clips | Face-track | Fallback | Bad FT | Conveyor |
|--------|-------|------------|----------|--------|----------|
| 16A (hand-segmented smoke) | 8 | 2 | 6 | 0 | 8/8 PASS |
| 17A / 18C (organic selection-gate, MFM) | 26 | 3 | 23 | 0 | 26/26 PASS |

Gate behaviour: **0 ineligible clips received face-track** across both batches.
Face-track rate ~11–25% depending on sample; low rate is expected.

---

## Smoke harness (single-clip QA)

```bash
cd video-automation
source .venv/bin/activate

python scripts/smoke/smoke_face_track_reframing.py \
  --input /path/to/clip.mp4 \
  --output-dir /tmp/face_track_smoke \
  --mode blur_background \
  --mode auto_test_disabled \
  --mode auto_test_enabled \
  --mode face_track
```

Or expand `--mode auto` with the test matrix:

```bash
python scripts/smoke/smoke_face_track_reframing.py \
  --input /path/to/clip.mp4 \
  --output-dir /tmp/face_track_smoke \
  --mode auto \
  --run-auto-test-matrix
```

---

## Quick reference checklist (printable)

```text
ENABLE (dev only):
  reframe_mode=auto, face_track_test_enabled=true

VERIFY BEFORE RUN:
  dev env · no upload · prod still blur+test off · disk space

RUN:
  20–30 organic selection-gate clips (or 5–10 smoke)

INSPECT:
  /ops/outputs → badges
  /ops/outputs/<job>/<clip> → Reframing section + preview
  /ops/jobs/<job> → summary line

GO IF:
  0 bad face-tracks · 0 metadata lies · captions OK · validation PASS

NO-GO IF:
  bad crop · caption overlap · UI/metadata mismatch · prod drift
```

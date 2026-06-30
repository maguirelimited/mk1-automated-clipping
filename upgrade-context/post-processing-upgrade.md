# Post-Processing Upgrade Plan

## MK1 Universal Conveyor → MK2 Creative Optimisation

---

# Purpose

The purpose of post-processing is **not** to find clips.

Processing has already done that.

Processing discovers, scores, and prepares raw clip candidates. Post-processing takes selected raw candidates and turns them into finished clips with the highest practical chance of performing well.

The objective is:

```text
Turn good raw clip candidates into stronger finished clips without duplicating the role of processing.
```

Performance means improving the practical chance of:

```text
higher average views
higher retention
higher engagement
higher conversion
eventually higher RPM
```

Post-processing should optimise realised performance.

It should not redo transcript analysis, rediscover clip candidates, or become a second processing engine.

---

# Core Boundary

The system must keep a clean boundary:

```text
Processing = raw potential discovery
Post-processing = selected candidate optimisation
```

Processing answers:

```text
Does this moment contain strong raw clip potential?
```

Post-processing answers:

```text
Which raw candidates should be rendered, universally upgraded, validated, and passed forward as finished clips?
```

Processing outputs:

```text
raw_candidate_pool.json
processing_report.json
```

Post-processing consumes:

```text
raw_candidate_pool.json
source video
post-processing config
job metadata
```

Post-processing outputs:

```text
finished clips
post_processing_report.json
per-clip metadata
```

---

# Design Principles

* Processing discovers raw potential.
* Post-processing optimises selected raw candidates.
* MK1 post-processing should be infrastructure-first, not overbuilt.
* MK1 should only include the universal conveyor.
* Universal upgrades should be high-ROI, repeatable, and low-judgement.
* There should be no optional upgrade layer in MK1.
* Deterministic modules execute edits.
* The LLM should not supervise post-processing edits in MK1.
* Every module should have a repeatable contract.
* Every module should be configurable, testable, and observable.
* MK1 validation should be PASS/FAIL, not an open-ended improvement loop.
* Post-processing should record lightweight metadata about what happened to each clip.
* The system should avoid fragile one-off editing scripts.
* Every MK1 stage should either produce the finished clip, improve its universal presentation quality, validate it, or record what happened.

---

# MK1 Active Architecture

This is the architecture to implement now:

```text
Raw Candidate Pool
        ↓
Selection Gate
        ↓
Universal Conveyor
        ↓
PASS/FAIL Validation
        ↓
Metadata + Report
        ↓
Finished Clip
```

MK1 does **not** include:

```text
optional upgrade modules
LLM inspection
specialist edit planning
B-roll insertion
face tracking
hook restructuring
intelligent zoom
ending optimisation
audio normalisation
silence trimming
pace adjustment
background music
sound effects
advanced dynamic captions
retention prediction
RPM optimisation
A/B edit generation
recursive quality loops
platform-specific creative strategies
```

Those are deferred.

The MK1 target is:

```text
select candidates
render clips
apply platform-safe formatting
apply intelligent captions
validate output
record what happened
```

---

# Stage 1 — Input Contract

Post-processing begins with the raw candidate pool produced by processing.

Expected input:

```text
raw_candidate_pool.json
```

Each candidate should already include:

```text
candidate ID
source section ID
start timestamp
end timestamp
duration
hook text
core idea summary
why the candidate has potential
archetype tag
confidence
rubric scores
warnings
transcript quality flags
```

Post-processing should treat this as the starting point.

It should not re-run clip discovery.

It should not re-analyse the full transcript unless a future debugging mode specifically requires it.

---

# Stage 2 — Selection Gate

The selection gate decides which raw candidates deserve rendering and universal conveyor processing.

Its purpose is to convert the raw candidate pool into a selected candidate set.

The selection gate should be driven by configuration, not hardcoded behaviour.

Supported MK1 selection modes:

```text
maximum_quality
balanced
maximum_data_collection
growth
custom
```

## Maximum Quality

Use when the system should prioritise only the strongest clips.

Behaviour:

```text
select fewer clips
require higher scores
allow clip slots to remain unused
avoid borderline candidates
prioritise confidence and clean boundaries
```

## Balanced

Use as the default mode.

Behaviour:

```text
select mostly strong candidates
allow limited exploration
respect max clip count
avoid weak or risky candidates
```

## Maximum Data Collection

Use when learning and volume matter more than immediate quality.

Behaviour:

```text
select more candidates
allow some borderline candidates
maximise data collection
still reject invalid timestamps or broken candidates
```

## Growth

Use when the system wants more output while maintaining basic quality.

Behaviour:

```text
increase clip volume
keep a minimum quality threshold
allow some exploration
avoid obviously weak clips
```

## Custom

Use explicit config values.

Example config:

```json
{
  "selection_mode": "balanced",
  "max_clips": 6,
  "min_overall_potential": 7,
  "min_confidence": 0.6,
  "respect_candidate_warnings": true
}
```

---

# Selection Gate Inputs

The selection gate should use:

```text
overall potential score
rubric scores
confidence
candidate duration
timestamp validity
candidate warnings
transcript quality flags
candidate archetype
funnel config
max clip count
quality threshold
selection mode
```

In MK1, selection should stay simple.

It should not attempt full social-media performance prediction.

It should rank and filter based on existing raw candidate evidence.

---

# Selection Gate Output

The selection gate should output:

```json
{
  "schema_version": "selection_gate_v1",
  "job_id": "string",
  "selection_mode": "balanced",
  "selected_candidates": [],
  "rejected_candidates": [],
  "reserve_candidates": [],
  "selection_summary": {
    "raw_candidates_received": 0,
    "selected_count": 0,
    "rejected_count": 0,
    "reserve_count": 0
  }
}
```

Each rejected or reserve candidate should include a reason.

Example reasons:

```text
below_quality_threshold
over_max_clip_count
invalid_timestamp
duration_too_short
duration_too_long
warning_too_strong
low_confidence
duplicate_candidate
not_enough_context
```

Reserve candidates are candidates that are valid but not selected.

They should be kept for later analysis or future use.

---

# Stage 3 — MK1 Universal Conveyor

Every selected candidate passes through the same universal conveyor.

There are no optional upgrades in MK1.

The active MK1 conveyor is:

```text
Selected Candidate
        ↓
render_clip_v1
        ↓
platform_safe_format_v1
        ↓
intelligent_captions_v1
        ↓
validation_v1
        ↓
metadata_writer_v1
```

Only two modules are true presentation/performance upgrades:

```text
platform_safe_format_v1
intelligent_captions_v1
```

The other modules are required infrastructure:

```text
render_clip_v1
validation_v1
metadata_writer_v1
```

This keeps the conveyor simple and high-ROI.

---

# Why These Are the MK1 Universal Modules

A true MK1 universal module should:

```text
apply to almost every selected clip
have clear ROI
not require heavy creative judgement
be deterministic enough to test
avoid creating more risk than value
fit the module framework
```

The two strongest universal upgrades are:

```text
platform-safe formatting
intelligent captions
```

These are worth building first because every short-form clip needs correct formatting and readable captions.

Other ideas may be valuable later, but they are not universal enough for MK1.

---

# Universal Module Framework

Each conveyor module should behave like a reusable module.

Every module should follow this contract:

```text
input clip/candidate
        ↓
module config
        ↓
processed output
        ↓
verification result
        ↓
module metadata
```

Each module should declare:

```text
name
version
input requirements
output path
config used
passed or failed
error reason if failed
runtime metadata
```

This avoids fragile one-off editing scripts and makes future upgrades easier.

---

# Standard Module Result

Each module should return a standard result object.

Example:

```json
{
  "module_name": "platform_safe_format_v1",
  "module_version": "1.0",
  "status": "PASS",
  "input_path": "string",
  "output_path": "string",
  "config": {},
  "error_reason": null,
  "warnings": [],
  "metadata": {}
}
```

Allowed statuses:

```text
PASS
FAIL
SKIPPED
```

In MK1, conveyor modules should normally be required.

If a required module fails, the clip should fail validation.

---

# Configurable Conveyor

The universal conveyor should be controlled by config.

Example:

```json
{
  "post_processing": {
    "enabled": true,
    "selection_mode": "balanced",
    "universal_conveyor": [
      "render_clip_v1",
      "platform_safe_format_v1",
      "intelligent_captions_v1",
      "validation_v1",
      "metadata_writer_v1"
    ]
  }
}
```

The system may support enabling, disabling, or reordering modules later, but the MK1 active conveyor should remain fixed until it is stable.

Do not introduce optional upgrades during MK1.

---

# Module 1 — render_clip_v1

## Purpose

Render the selected timestamp range from the source video.

This is infrastructure, not a performance upgrade.

## Responsibilities

```text
cut source video using selected candidate timestamps
write initial clip file
preserve audio/video sync
record render path
record render status
```

## Verification

```text
output file exists
duration is close to expected
file is readable
ffmpeg did not fail
audio/video sync is not obviously broken
```

---

# Module 2 — platform_safe_format_v1

## Purpose

Prepare the clip for short-form platforms.

This replaces the older idea of basic vertical formatting.

The goal is not just “make it vertical.” The goal is to make the clip safe and usable for Shorts, Reels, TikTok-style layouts.

## Responsibilities

```text
convert or format output to 9:16 vertical
avoid distorted scaling
preserve important visual content as much as possible
use consistent export resolution
leave safe zones for captions
avoid placing key content where platform UI usually appears
ensure output is suitable for short-form distribution
```

## Verification

```text
output file exists
video stream exists
aspect ratio is correct
resolution is correct
duration remains valid
file is playable
no obvious stretching or broken scaling
```

## MK1 Constraint

MK1 should use a simple deterministic formatting strategy.

Do not add advanced face tracking, object tracking, or creative reframing here.

Those belong later.

---

# Module 3 — intelligent_captions_v1

## Purpose

Add readable, well-positioned captions that improve retention and comprehension.

This replaces the older idea of basic subtitles.

Captions are one of the highest-ROI universal upgrades because short-form viewers often watch with low sound, no sound, or distracted attention.

## Responsibilities

```text
generate captions from the transcript segment
align captions to speech timing
use readable font size
use safe caption positioning
avoid covering important visual areas where practical
use sensible line breaks
avoid overly long caption blocks
support platform-safe margins
optionally highlight important words if simple and deterministic
write captioned output
```

## Verification

```text
caption data exists
caption timing is valid
caption text is not empty
output file exists
duration remains valid
file is playable
captions appear within safe visual area
```

## MK1 Constraint

Captions should be intelligent, but not overbuilt.

MK1 should avoid:

```text
complex kinetic caption systems
LLM-written caption rewrites
heavy animation logic
style experiments
per-clip creative caption planning
```

The first version should prioritise readability, timing, and safe placement.

---

# Module 4 — validation_v1

## Purpose

Decide whether the final clip passes basic MK1 technical checks.

Validation is infrastructure, not a performance upgrade.

## Responsibilities

```text
check file exists
check file is playable
check duration is valid
check required modules completed
check no required module failed
check output path is recorded
return PASS or FAIL
```

Validation should not create a recursive improvement loop in MK1.

## PASS/FAIL Output

The result should be:

```text
PASS
```

or:

```text
FAIL
```

If validation fails, the clip should be marked failed or rejected cleanly.

Do not do this in MK1:

```text
fail
↓
ask LLM for revised edit plan
↓
rerun specialist tools
↓
inspect again
```

That loop belongs to MK2.

---

# Module 5 — metadata_writer_v1

## Purpose

Record what happened to each clip.

This is infrastructure, not a performance upgrade.

## Responsibilities

```text
write selected candidate ID
write input candidate scores
write module list
write module versions
write module configs
write module results
write validation result
write output file path
write failure reason if failed
write warnings
```

This creates useful data for debugging and later optimisation without building a full optimisation database yet.

---

# Stage 4 — Lightweight Per-Clip Metadata

Each finished clip should carry basic metadata.

Minimum per-clip metadata:

```json
{
  "clip_id": "string",
  "source_candidate_id": "string",
  "source_video_path": "string",
  "input_start_sec": 0.0,
  "input_end_sec": 0.0,
  "input_duration_sec": 0.0,
  "input_candidate_scores": {},
  "input_candidate_archetype": "string",
  "selection_mode": "balanced",
  "modules_applied": [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
    "validation_v1",
    "metadata_writer_v1"
  ],
  "module_versions": {},
  "module_configs": {},
  "module_results": [],
  "validation_result": "PASS",
  "output_file_path": "string",
  "failure_reason": null,
  "warnings": []
}
```

This metadata should be stored alongside the finished clip or inside the post-processing report.

---

# Stage 5 — Post-Processing Report

Each job should output:

```text
post_processing_report.json
```

Minimum structure:

```json
{
  "schema_version": "post_processing_report_v1",
  "job_id": "string",
  "post_processing_version": "post_processing_mk1_v1",
  "selection_mode": "balanced",
  "raw_candidates_received": 0,
  "candidates_selected": 0,
  "reserve_candidates": 0,
  "candidates_rejected": 0,
  "clips_rendered": 0,
  "clips_passed": 0,
  "clips_failed": 0,
  "modules_run": [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
    "validation_v1",
    "metadata_writer_v1"
  ],
  "failed_modules": [],
  "finished_clip_paths": [],
  "failed_clips": [],
  "rejected_candidates": [],
  "warnings": []
}
```

The report should make it obvious:

```text
how many candidates entered
how many were selected
how many were rendered
how many passed
how many failed
which modules ran
why anything failed
where finished clips are stored
```

---

# Things Deliberately Not Owned by Post-Processing

Post-processing should not own:

```text
transcription
WhisperX execution
transcript chunking
raw candidate discovery
raw transcript potential judgement
global job truth
source downloading
uploading
scheduling
output-funnel state
platform account management
posting strategy
```

Those responsibilities belong elsewhere.

Post-processing should only own:

```text
candidate selection
universal conveyor execution
technical validation
clip-level metadata
post-processing report
finished clip handoff
```

---

# Deferred Improvements

The following improvements are **not optional MK1 modules**.

They are deferred.

They should not be built during the first MK1 universal conveyor implementation.

```text
silence trimming / pace adjustment
face tracking / reframing
hook restructuring
intelligent zoom
audio normalisation
ending optimisation
B-roll
colour correction
audio cleanup
music selection
sound effects
retention prediction
RPM optimisation
A/B edit generation
platform-specific creative editing
```

## Why They Are Deferred

### Silence trimming / pace adjustment

Potentially useful, but risky.

It can damage natural speech flow, cut meaningful pauses, or create awkward pacing if done badly.

### Face tracking / reframing

Can be valuable later, but not safe enough as a universal MK1 module.

It can crop badly, fail with multiple people, or make clips worse.

### Hook restructuring

Requires creative judgement.

This belongs to an LLM-supervised specialist layer, not the MK1 universal conveyor.

### Intelligent zoom

Can improve energy, but can also look cheap or distracting.

Not universal enough for MK1.

### Audio normalisation

Can help if source audio is poor, but it adds ffmpeg complexity and can introduce clipping, distortion, or debugging pain.

Not worth including in the first conveyor unless test clips prove it is needed later.

### Ending optimisation

Requires judgement about the idea, rhythm, and final moment of the clip.

Defer until specialist edit planning exists.

### B-roll

Requires asset selection, relevance judgement, timing, and visual style control.

Definitely not an MK1 universal module.

---

# Deferred MK2 Architecture

The following architecture is valuable, but it is **not** part of MK1:

```text
Universal Conveyor
        ↓
LLM Inspection
        ↓
Specialist Toolbox
        ↓
Quality Inspection
        ↓
Optional Further Edits
        ↓
Finished Clip
```

In MK2, the LLM can act as a creative supervisor.

It should still not directly edit videos.

The pattern should be:

```text
Inspect
↓
Plan
↓
Execute deterministic modules
↓
Validate
↓
Approve
```

The LLM decides which specialist tools are useful.

Deterministic modules execute the actual edits.

---

# Deferred MK2 Specialist Tools

Possible MK2 specialist tools include:

```text
silence trimming / pace adjustment
face tracking / reframing
hook restructuring
intelligent zoom
audio normalisation
ending optimisation
B-roll insertion
diagram insertion
screenshot insertion
keyword highlighting
emphasis animations
sound effects
background music selection
visual emphasis
callout graphics
timeline graphics
statistic overlays
AI-generated B-roll
AI-generated diagrams
advanced dynamic captions
object tracking
emotion detection
thumbnail frame selection
retention prediction
RPM optimisation
A/B edit generation
platform-specific edit strategies
```

These should plug into the same module framework created in MK1.

That is why MK1 must build the conveyor cleanly.

---

# Future Expansion Principle

MK1 should establish the structure that MK2 can extend.

The system should eventually support:

```text
universal modules
specialist modules
LLM-planned edit decisions
platform-specific variants
funnel-specific optimisation
performance feedback loops
A/B testing
RPM optimisation
```

But none of that should be built before the MK1 conveyor is stable.

The foundation matters more than adding creative complexity early.

---

# MK1 Implementation Order

Implement post-processing in this order:

## 1. Post-processing entrypoint

Create the service/function that consumes:

```text
raw_candidate_pool.json
source video path
job metadata
post-processing config
```

and produces:

```text
finished clips
post_processing_report.json
clip metadata
```

---

## 2. Selection gate v1

Implement candidate filtering and ranking based on:

```text
selection mode
max clip count
overall potential
confidence
warnings
duration validity
timestamp validity
```

Output selected, rejected, and reserve candidates.

---

## 3. Universal module framework

Create the standard module contract.

Every module should return:

```text
module name
version
status
input path
output path
config
warnings
error reason
metadata
```

---

## 4. Fixed MK1 universal conveyor

Implement the fixed MK1 conveyor:

```text
render_clip_v1
platform_safe_format_v1
intelligent_captions_v1
validation_v1
metadata_writer_v1
```

Do not add optional upgrade modules.

---

## 5. render_clip_v1

Render selected candidates from source video.

This can reuse existing clipping logic if available.

---

## 6. platform_safe_format_v1

Format the clip safely for short-form platforms.

This is one of the two real MK1 universal upgrades.

---

## 7. intelligent_captions_v1

Add readable, well-timed, platform-safe captions.

This is one of the two real MK1 universal upgrades.

---

## 8. validation_v1

Add PASS/FAIL validation.

Reject or fail clips cleanly.

Do not recursively improve clips.

---

## 9. metadata_writer_v1

Write per-clip metadata and module results.

---

## 10. post_processing_report.json

Write the job-level post-processing report.

---

## 11. End-to-end integration

Connect:

```text
processing raw_candidate_pool.json
↓
post-processing selection gate
↓
universal conveyor
↓
finished clips
↓
output-funnel registration
```

---

# MK1 Completion Criteria

Post-processing MK1 is complete when:

```text
it consumes raw_candidate_pool.json
it selects candidates without rediscovering clips
it renders selected clips
it runs the fixed MK1 universal conveyor
it applies platform-safe formatting
it applies intelligent captions
it validates outputs with PASS/FAIL
it writes per-clip metadata
it writes post_processing_report.json
it passes finished clips forward to the existing output path
it fails cleanly when a clip or module fails
```

A successful job should leave behind:

```text
raw_candidate_pool.json
processing_report.json
finished clip files
per-clip metadata
post_processing_report.json
main job report with paths to all important outputs
```

---

# Non-Negotiables

Do not let post-processing become another clip discovery system.

Do not make post-processing responsible for transcription.

Do not make post-processing responsible for global job truth.

Do not build optional upgrade modules in MK1.

Do not build the MK2 LLM/specialist loop during MK1.

Do not create fragile one-off editing scripts outside the module framework.

Do not skip validation.

Do not skip metadata.

Do not skip reporting.

Do not break the existing production path without a compatibility adapter.

The MK1 objective is:

```text
take raw candidates
select the best valid candidates
render clips
apply platform-safe formatting
apply intelligent captions
validate outputs
record what happened
produce finished clips
```

Once this works reliably, MK2 can add LLM inspection, specialist tools, creative optimisation, feedback loops, and platform-specific performance improvements.

# Processing Plan (MK1)

## Purpose

The purpose of processing is **not** to produce the final clips.

Its purpose is to discover, evaluate and prepare the **best raw clip candidates** that can later be optimised by post-processing.

Processing should answer:

> **"Does this part of the source contain strong raw clip potential?"**
> 

It should **not** answer:

> **"Will this finished clip perform best on social media?"**
> 

That responsibility belongs to post-processing.

---

# Design principles

- AI judges; deterministic code executes.
- Processing measures **raw potential**, not final performance.
- Final optimisation belongs to post-processing.
- Avoid duplicate work between processing and post-processing.
- Produce rich evidence that later stages can use rather than making irreversible decisions.
- Processing should favour explainable evidence over opaque decisions.
- The output of processing is a **raw candidate pool**, not publishable clips.

---

# Current architecture

```
Source Video
      ↓
WhisperX
      ↓
Transcript
      ↓
Hierarchical transcript analysis
      ↓
Section candidate discovery
      ↓
Raw potential scoring
      ↓
Timestamp overlap control
      ↓
Raw candidate pool
```

---

# Current processing upgrades

## 1. Hierarchical transcript analysis ✅

Large transcripts are split into bounded sections.

Each section is analysed independently.

This improves:

- consistency
- scalability
- inference speed
- memory usage
- parallelisation opportunities

The AI should judge one bounded section at a time rather than an entire podcast.

---

## 2. Candidate discovery instead of final selection ✅

Each section performs a **scouting pass**.

Its job is to surface promising standalone clip candidates.

It is **not** responsible for deciding the final clips for posting.

Instead it produces raw candidates that can later be compared and optimised.

---

## 3. Rubric-backed potential scoring ✅

Candidates are scored using a structured rubric instead of a single arbitrary number.

Example dimensions include:

- hook strength
- standalone context
- insight value
- retention potential
- natural ending
- overall potential

These scores represent **raw clip potential**, not expected social-media performance.

They exist to provide evidence for later stages rather than acting as the final judgement.

---

## 4. Boundary sanity pass (planned)

Processing should ensure candidate timestamps are fundamentally sound.

Examples:

- doesn't begin halfway through a sentence
- doesn't end halfway through an idea
- stays inside transcript bounds
- contains sufficient surrounding context

Processing should **not** aggressively optimise the exact start and end timestamps for performance.

Fine-grained timing optimisation belongs to post-processing.

---

## 5. Timestamp overlap control (planned)

Neighbouring transcript sections overlap intentionally.

Processing should therefore prevent duplicate candidates that represent the same timestamp range.

This should only remove **true timestamp duplicates**.

It should **not** remove clips simply because they discuss similar ideas.

Different moments in a podcast may legitimately produce similar but distinct clips.

---

## 6. Funnel-specific prompt framework (planned)

The processing engine should support funnel-specific prompt variants and rules.

Each funnel may have its own version of the core prompt while sharing the same overall architecture.

Examples:

- Business
- Finance
- Sport
- Comedy

This provides flexibility without creating separate processing systems.

---

## 7. Raw candidate evidence package (planned)

Every candidate should carry enough structured evidence for later stages.

The goal is to help post-processing understand **why** the candidate exists without repeating transcript analysis.

Possible evidence includes:

- hook text
- core idea summary
- why the candidate has potential
- source section
- confidence
- rubric scores

Processing should capture evidence, not final conclusions.

---

## 8. Candidate archetype tagging (planned)

Processing should lightly classify what type of opportunity each candidate represents.

Examples:

- valuable insight
- funny moment
- controversial opinion
- story
- explanation
- emotional moment
- surprising fact

These are descriptive tags rather than final publishing strategy.

They provide useful context for future optimisation.

---

## 9. Transcript quality awareness (planned)

Processing should recognise when poor transcript quality may affect judgement.

Possible flags include:

- low transcript confidence
- speaker confusion
- missing words
- timestamp uncertainty
- unclear audio

This allows later stages to distinguish between poor clips and poor source data.

---

## 10. Processing diagnostics (planned)

Each processing job should generate a lightweight diagnostic report.

Examples:

- sections analysed
- usable sections
- rejected sections
- candidates discovered
- transcript warnings
- processing warnings
- common rejection reasons

The purpose is to improve observability and make the clipping engine easier to debug and benchmark.

---

# Things deliberately left to post-processing

Processing should **not** own:

- final clip ranking for publication
- final start/end optimisation
- expected performance optimisation
- platform-specific optimisation
- editing decisions
- clip count strategy
- exploration vs exploitation strategy
- posting strategy
- visual improvements
- captions
- B-roll
- music
- graphics
- edit planning

Those belong to the post-processing system.

---

# Final principle

Processing is a **raw potential engine**.

Its responsibility ends once it has produced the highest-quality, well-supported pool of raw clip candidates.

Those candidates should contain enough evidence, metadata and structure that post-processing can optimise them without having to rediscover them from the transcript.

Keeping processing focused on **discovery** and post-processing focused on **optimisation** creates a clean architectural boundary, reduces duplicated AI decision-making, and makes both systems easier to improve independently over time.
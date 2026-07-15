---

# 1. Redesign Transcript Presentation for Discovery

I actually think this is one of the most important upgrades because it fundamentally changes what Discovery is capable of.

Rather than thinking about "chunking", I think it's more accurate to think about **how the transcript is presented to Discovery**.

"Chunking" is simply one implementation of the transcript presentation strategy. The real design question is how to present the transcript so the local model has the best possible chance of identifying genuinely good candidate moments while operating within its context window.

---

## The problem today

The current presentation strategy has several limitations.

### 1. Context is limited

Stories, explanations and discussions often span several minutes.

If Discovery only sees a small portion of the transcript, it may understand the setup but not the payoff, or the payoff without the setup.

This reduces its ability to recognise genuinely interesting moments.

---

### 2. Conversations are split artificially

Partition boundaries are primarily determined by transcript length rather than conversational flow.

As a result, complete ideas can be divided across multiple partitions, preventing Discovery from understanding them as a whole.

---

### 3. Good candidates may be lost early

Discovery currently operates independently on each transcript partition.

If each partition is limited in how many candidates it can return, interesting moments can be discarded before later stages ever have the opportunity to evaluate them.

---

### 4. The presentation strategy should optimise Discovery

The local model's context window is a hard constraint, so transcript partitioning will always exist.

The goal is therefore **not** to eliminate partitions.

The goal is to design a partitioning strategy that gives Discovery the highest possible chance of recognising complete candidate moments while remaining within the usable context window of the local model.

---

# Design principles

The transcript presentation strategy should follow a few simple principles.

### Fixed partition sizes

Use fixed transcript partitions whose size is determined by the usable context window of the local model.

This makes behaviour predictable, easy to benchmark and inexpensive to process.

---

### Fixed overlap

Neighbouring partitions should overlap by a fixed amount.

The purpose of the overlap is to ensure candidate clips that cross a partition boundary can still exist in at least one complete partition.

Given the intended maximum clip length (~2 minutes), an overlap of approximately 60 seconds on both sides is currently considered the preferred design.

---

### Natural boundaries

Partitions should snap to natural transcript boundaries where possible.

For example:

- Whisper segments
- sentence boundaries

rather than cutting sentences or speech in half.

This improves readability without requiring another AI model to perform the partitioning.

---

### Keep partitioning deterministic

Partitioning should remain a lightweight deterministic process.

It should not require additional LLM inference or subjective reasoning.

Its only responsibility is presenting the transcript effectively for Discovery.

---

# What we need to decide

These are the remaining architectural decisions.

- What partition size best utilises the local model's context window?
- Is ~60 seconds of overlap the correct amount in practice?
- Which transcript boundaries should partitions snap to?
- Should any additional deterministic rules improve partition quality?

---

# What we should not decide yet

This discussion should not lock in exact implementation values until they have been tested.

For example:

- Exact partition duration
- Exact overlap duration
- Exact token budgets

These should be validated through experimentation once the architecture is implemented.

---

# Design goal

> The transcript presentation strategy should maximise Discovery's ability to identify complete candidate moments while remaining within the constraints of the local model. It should use fixed, deterministic partitions with sufficient overlap to preserve candidate clips across boundaries, respect natural transcript boundaries where practical, and remain simple enough to benchmark and improve over time.
> 

---

# Layman's summary

**What changes?**

- Replace the current transcript partitioning strategy.
- Keep **fixed-size partitions** because the local model requires them.
- Size the partitions around the **local model's usable context window**, not arbitrary time values.
- Add **fixed overlap** so clips spanning partition boundaries can still be discovered.
- Stop partitions from cutting through sentences where possible.
- Keep the partitioning process **deterministic and lightweight**—no extra AI model required.
- Design the presentation specifically to help Discovery find better candidate moments, not simply to minimise processing.


I think we've converged on something much cleaner than where we started.

One thing I have deliberately removed is any discussion about **what a candidate object looks like**. That's the next architectural point ("Replace clip outputs with candidate moments"). If we define it here, the two discussions start overlapping.

---

# 2. Replace Discovery Prompt and Logic

I actually think this is the biggest architectural change in the entire clip selection pipeline.

The goal is not simply to write a better prompt.

The goal is to redefine **Discovery's responsibility**.

Today, Discovery effectively tries to solve the entire clip selection problem in a single step.

Instead, Discovery should become a specialised stage whose only responsibility is identifying every transcript moment with genuine potential to become a high-quality short-form clip.

---

## The problem today

The current Discovery stage has several architectural problems.

### 1. Discovery is too selective

Discovery currently behaves like the final editor.

It attempts to identify only the "best" clips, meaning potentially good candidates are discarded immediately.

Since later stages never see rejected candidates, they cannot recover them.

---

### 2. Discovery tries to solve too much

Discovery currently attempts to make a final judgement using only the transcript.

However, at this stage it has no knowledge of:

- visual quality
- edit opportunities
- reframing
- facial expressions
- pacing
- audio characteristics

It is therefore making a final decision using incomplete information.

---

### 3. Discovery becomes the pipeline bottleneck

Anything rejected by Discovery disappears from the pipeline entirely.

This means the overall quality of the system is heavily constrained by one AI prompt operating on incomplete information.

---

### 4. Discovery is optimised for precision instead of recall

The current design encourages the model to return only a small number of candidates.

For this architecture, missing a genuinely good candidate is far more costly than returning an additional reasonable candidate.

The optimisation target should therefore shift from **precision** to **recall**.

---

# New responsibility

Discovery should have one responsibility:

> Read a transcript partition and identify every moment that has genuine potential to become a successful short-form clip.
> 

Its job is **not** to decide whether the candidate should ultimately be rendered.

That responsibility belongs to a later evaluation stage.

---

# Design principles

### Maximise recall

Discovery should favour returning an additional reasonable candidate over missing a genuinely good one.

False positives can be filtered later.

False negatives are lost permanently.

---

### Evaluate transcript content only

Discovery should only evaluate information that actually exists within the transcript.

It should not attempt to infer information that will later be measured objectively elsewhere in the pipeline.

---

### Funnel-driven discovery

Discovery should not use one universal definition of an interesting candidate.

Instead, each funnel should define what qualifies as a candidate through its own prompts, rules or configuration.

A business podcast, gaming stream and comedy podcast should naturally produce different types of candidates.

---

### Independent partition processing

Each transcript partition should be analysed independently.

Discovery should not attempt to compare candidates across different transcript partitions.

Cross-partition comparison belongs later in the pipeline.

---

# What we need to decide

These are the remaining architectural decisions.

- How should funnels define what qualifies as a candidate?
- How conservative should Discovery be when uncertainty exists?
- What minimal information should Discovery return for downstream processing?

---

# What we should not decide yet

This discussion should not define implementation details such as:

- exact prompt wording
- prompt examples
- JSON schema
- model parameters
- temperature
- output formatting

Those belong in the implementation stage.

---

# Design goal

> Discovery should maximise recall by identifying every transcript moment with genuine short-form potential. It should operate only on transcript information, follow funnel-specific discovery rules, avoid making final selection decisions, and pass candidate moments to later stages for further processing and evaluation.
> 

---

# Layman's summary

**What changes?**

- Discovery stops trying to find the **best clips**.
- Discovery starts finding **every genuinely interesting moment**.
- Discovery becomes **less selective**.
- Discovery no longer makes final clip decisions.
- Different funnels can define different discovery criteria.
- Discovery only uses transcript information available at that stage.
- Final selection is moved to a dedicated evaluation stage later in the pipeline.

---

## One small wording change I'd make

I actually wouldn't call this **"Replace Discovery Prompt and Logic"** anymore.

I'd call it:

> **Redesign Discovery Stage**
> 

The reason is that you're changing far more than the prompt. You're changing:

- its purpose,
- its optimisation target,
- its decision-making philosophy,
- and its role within the overall architecture.

The prompt is just one implementation of that new design. I think "Discovery Stage" better captures the scope of what you're redesigning.



I think this is now at the right level. It defines the architectural change without drifting into the processing infrastructure or candidate schema.

---

# 3. Replace Clip Outputs with Candidate Objects

The Discovery stage should no longer return finished clips.

Instead, it should return **candidate objects** representing potential clips that continue through the remainder of the pipeline before a final decision is made.

This changes Discovery from producing a final output into producing the input for the next stages of processing.

---

## The problem today

### 1. Discovery returns finished clips

Today, Discovery effectively returns clips that are treated as final outputs.

This means the first AI stage immediately commits to clip boundaries and selection decisions before any further analysis has taken place.

---

### 2. Later stages cannot improve candidates

Once a clip has been returned, there is little opportunity for later stages to analyse, enrich or reject it.

The pipeline should instead allow potential clips to continue through additional processing before a final selection is made.

---

## New responsibility

Discovery should return **candidate objects**, not finished clips.

A candidate object simply represents:

> **"This section of the transcript has potential to become a clip."**
> 

It is not yet an approved or final clip.

---

## Design principles

### Candidates are not finished clips

A candidate is simply a possible clip that deserves further processing.

It should never be treated as the final output of Discovery.

---

### One candidate throughout the pipeline

The same candidate object should move through every remaining stage of the pipeline.

Rather than creating replacement objects, each stage should enrich or refine the existing candidate by adding the information it uniquely knows.

This creates one canonical representation of the candidate throughout the pipeline.

---

### Keep the initial candidate lightweight

Discovery should only return the information it actually knows.

Additional information should be added by later stages as the candidate progresses through the pipeline.

---

## What we need to decide

- What minimum information should a candidate contain immediately after Discovery?
- At what point does a candidate officially become a rendered clip?

---

## What we should not decide yet

This discussion should not define:

- the complete candidate schema
- JSON structure
- enrichment fields
- storage format
- processing implementation

Those belong to the following architectural discussions.

---

## Design goal

> Discovery should return lightweight candidate objects representing potential clips rather than finished clips. These candidate objects become the canonical unit flowing through the remainder of the pipeline, with each stage enriching the same object until the final evaluation decides whether it should become a rendered clip.
> 

**The candidate object should follow a single canonical schema that is shared across every stage of the pipeline.**

---

# Layman's summary

**What actually changes?**

- Discovery **stops returning finished clips**.
- Discovery **starts returning candidate objects**.
- A candidate is simply **"this might become a clip."**
- The **same candidate object** flows through the rest of the pipeline.
- Each later stage **adds information to the same candidate**, rather than creating a new one.
- A candidate only becomes a real clip after the evaluation stage approves it.



---

# 4. Add Candidate Processing Infrastructure

The pipeline should introduce a single **candidate processing stage** between Discovery and Evaluation.

This stage becomes the canonical place where all candidate-level processing takes place before a final selection is made.

The goal is **not** to build a new processing framework. The goal is to reorganise the existing processing flow so that candidate processing has one clear home within the architecture.

---

## The problem today

### 1. Candidate processing is not a defined stage

As new processing steps are added, they risk becoming scattered throughout the pipeline.

This makes the processing flow harder to understand, maintain and extend.

---

### 2. Future enrichments have no obvious place

Features such as:

- emotion analysis
- face analysis
- audio analysis
- future candidate enrichments

should not be inserted wherever convenient.

They should all belong to one well-defined stage within the pipeline.

---

### 3. Avoid duplicate infrastructure

The system already contains processing infrastructure.

The goal should therefore be to reorganise and extend the existing processing flow where practical, rather than introducing a second parallel processing framework that increases maintenance and debugging complexity.

---

# New responsibility

Introduce a dedicated **Candidate Processing** stage whose responsibility is to perform all processing that operates on candidate objects before they reach Evaluation.

Initially this stage may contain only a small number of processors such as:

- merge overlapping candidates
- deduplicate candidates

The architecture should allow additional processors to be added over time without requiring further pipeline redesign.

---

# Design principles

### One canonical processing stage

All candidate-level processing should occur within a single, well-defined stage of the pipeline.

This creates one clear location for future processing capabilities.

---

### Reuse existing infrastructure

Where practical, existing processing logic should be reorganised into the new stage rather than rewritten.

The redesign should minimise duplicated infrastructure and unnecessary refactoring.

---

### Independent processors

Each processor should have one clearly defined responsibility.

For example:

- merge candidates
- deduplicate candidates
- emotion analysis
- face analysis

Each processor should perform its own task without becoming responsible for unrelated processing.

---

### Extensible architecture

New candidate processors should be easy to add without modifying the overall pipeline structure.

Future enrichments should integrate into the existing processing stage rather than requiring additional architectural changes.

---

# What we need to decide

- Which existing processing logic should become part of Candidate Processing?
- Which responsibilities belong inside this stage?
- In what order should processors execute?

---

# What we should not decide yet

This discussion should not define:

- individual enrichments
- processor implementations
- execution framework
- plugin system
- internal APIs

Those belong to later implementation work.

---

# Design goal

> Create one canonical Candidate Processing stage that performs all processing on candidate objects between Discovery and Evaluation. The stage should reuse existing infrastructure where practical, provide a clear home for future processing capabilities, and remain simple to extend without introducing duplicate processing frameworks.
> 

---

# Layman's summary

**What actually changes?**

- Add a dedicated **Candidate Processing** stage to the pipeline.
- Make this the **only place** where candidate-level processing happens.
- Reuse and reorganise existing processing code where possible instead of building a second framework.
- Initially include only the essential processors (such as merge and deduplication).
- Make it easy to add future processors (emotion, face analysis, audio analysis, etc.) without redesigning the pipeline.
- Avoid creating duplicate infrastructure that increases maintenance or debugging complexity.


I think this is the right scope. It defines the new stage without drifting into how the evaluator will actually work.

---

# 5. Replace Single-Stage Selection with a Dedicated Evaluation Stage

The pipeline should no longer rely on Discovery to make the final clip selection.

Instead, a dedicated **Evaluation** stage should become responsible for deciding which candidate objects ultimately become rendered clips.

This separates discovering potential clips from deciding which ones should actually be produced.

---

## The problem today

### 1. Discovery makes the final decision

Discovery currently identifies potential clips and immediately decides which ones should become outputs.

This means the first AI stage is responsible for both finding and selecting clips.

---

### 2. Decisions are made using incomplete information

Discovery only has access to transcript information.

It cannot use additional information that later stages may produce, such as future enrichments or candidate processing results.

As a result, final selection decisions are made before the pipeline has gathered all available evidence.

---

### 3. Responsibilities are combined

Discovery is currently responsible for:

- finding candidates
- rejecting candidates
- selecting final clips

These are separate responsibilities and should not belong to a single stage.

---

# New responsibility

Introduce a dedicated **Evaluation** stage whose only responsibility is deciding which processed candidates should become rendered clips.

Evaluation should operate only on candidate objects that have already completed Discovery and Candidate Processing.

---

# Design principles

### Discovery discovers

Discovery identifies potential candidate clips.

It does not make the final decision.

---

### Processing enriches

Candidate Processing gathers additional information about each candidate.

It does not decide which candidates survive.

---

### Evaluation decides

Evaluation is the only stage responsible for accepting or rejecting candidates.

It makes the final selection using all information already attached to the candidate object.

---

### Clear separation of responsibilities

Evaluation should not:

- rediscover candidates
- perform candidate processing
- render clips

Its responsibility is limited to making the final selection decision.

---

# What we need to decide

- What information should Evaluation consider when making decisions?
- Can Evaluation adjust candidate boundaries, or should it only decide whether to accept or reject them?
- How many clips should Evaluation ultimately select?

---

# What we should not decide yet

This discussion should not define:

- evaluation prompt wording
- scoring methodology
- ranking algorithms
- AI implementation details
- model configuration

Those belong to later implementation work.

---

# Design goal

> Introduce a dedicated Evaluation stage that becomes solely responsible for selecting which processed candidate objects should become rendered clips. This separates discovery from decision-making, allowing final selections to be made using the complete information gathered throughout the pipeline while maintaining clear responsibilities between stages.
> 

---

# Layman's summary

**What actually changes?**

- Discovery **stops deciding** which clips get rendered.
- A new **Evaluation** stage is added after Candidate Processing.
- Evaluation becomes the **only stage** that decides whether a candidate becomes a real clip.
- Processing gathers information; Evaluation uses that information to make the final decision.
- Each stage now has one clear responsibility:
    - **Discovery** → Find candidates.
    - **Candidate Processing** → Analyse and enrich candidates.
    - **Evaluation** → Choose the final clips.


    I think this one is actually very simple.

It's not:

> "Remove scoring."
> 

It's:

> **Remove arbitrary scoring.**
> 

That's the important distinction.

If, six months from now, you have enough data to build a statistically validated scoring model, that's a completely different discussion.

Today, you're removing something because it isn't grounded in evidence.

I also wouldn't say "never score."

I'd say:

> **Don't invent scores that nobody can justify.**
> 

---

# 6. Remove Rubric and Weighted Scoring

The clip selection pipeline should no longer rely on manually designed rubric scores or weighted scoring systems to determine clip quality.

Instead, final selection should be performed by the dedicated Evaluation stage using all available information gathered throughout the pipeline.

---

## The problem today

### 1. The weights are arbitrary

A weighted scoring system requires deciding things such as:

- emotion = 25%
- story = 30%
- pacing = 15%

There is currently no evidence that these weights accurately predict clip performance.

---

### 2. The AI cannot justify the weights either

Asking an AI to invent or adjust weighting values does not solve the problem.

Without real performance data, the weights remain subjective rather than evidence-based.

---

### 3. Weighted scoring becomes difficult to maintain

As new processing capabilities are introduced, every new feature requires deciding:

- should it contribute to the score?
- how much should it contribute?

This makes the scoring system increasingly complex without guaranteeing better clip selection.

---

# New responsibility

The architecture should no longer depend on manually weighted scoring to determine which clips are selected.

Instead, Evaluation should make the final selection using the complete candidate object and the information already attached to it.

---

# Design principles

### Remove arbitrary weighting

Do not introduce manually assigned weights unless they are supported by evidence.

---

### Let Evaluation make the decision

Evaluation should consider the complete candidate rather than reducing it to a manually calculated score.

---

### Future evidence can change this

If future production data demonstrates that certain measurable signals genuinely predict clip performance, a data-driven scoring system can be considered.

This redesign does not prevent that.

It simply avoids introducing unsupported weighting today.

---

# What we need to decide

Nothing.

The architectural decision is simply to remove rubric-based selection from the pipeline.

---

# What we should not decide yet

This discussion should not define:

- future learning systems
- performance analytics
- statistical models
- machine learning approaches

Those belong to a future redesign once sufficient production data exists.

---

# Design goal

> Remove manually weighted rubric scoring from the clip selection pipeline and rely on the dedicated Evaluation stage to make final selection decisions using the complete candidate object. Any future scoring system should be driven by real performance data rather than assumptions.
> 

---

# Layman's summary

**What actually changes?**

- Remove the manually weighted rubric system.
- Stop trying to calculate clip quality using arbitrary percentages or point values.
- Let the Evaluation stage make the final decision instead.
- Leave the door open for future data-driven scoring once enough real-world performance data has been collected.

---

I also think this is one of the few sections where **"What we need to decide"** can genuinely be empty.

The decision has already been made:

> **Don't build a fake mathematical model just because it feels more objective.**
> 

That's the entire architectural change. It doesn't need to grow into another design discussion.


I think this one is actually even simpler than it sounds.

The prompts aren't really the architecture.

They're just the implementation of everything you've just designed.

So I'd be careful not to turn this into another prompt design discussion.

---

# 7. Replace Prompts to Fit the New Architecture

The AI prompts should be redesigned to reflect the responsibilities of the new pipeline.

Rather than one prompt attempting to solve the entire clip selection problem, each prompt should be responsible only for the stage in which it operates.

The prompts should reinforce the architectural separation between Discovery, Candidate Processing and Evaluation.

---

## The problem today

### 1. The prompts reflect the old architecture

The current prompts were designed around a single-stage clip selection process.

As the architecture evolves, the prompts no longer match the responsibilities of each stage.

---

### 2. Responsibilities are blurred

The current prompts encourage one AI stage to perform multiple responsibilities simultaneously.

This makes prompt tuning more difficult and reduces the clarity of each stage's purpose.

---

## New responsibility

Each AI prompt should be responsible only for the stage in which it operates.

The prompts should support the architecture rather than define it.

---

## Design principles

### One responsibility per prompt

Each prompt should have one clearly defined responsibility.

For example:

- Discovery identifies candidate clips.
- Evaluation decides which candidates become rendered clips.

No prompt should attempt to perform multiple architectural responsibilities.

---

### Prompts follow the architecture

The prompts should reflect the agreed pipeline rather than introducing new behaviour or responsibilities.

Changes to the architecture should drive prompt changes, not the other way around.

---

### Keep prompts focused

Each prompt should contain only the information required for its stage.

Avoid mixing unrelated responsibilities into a single prompt.

---

## What we need to decide

- What information should each stage receive?
- What information should each stage return?

These decisions should follow naturally from the architectural responsibilities already defined.

---

## What we should not decide yet

This discussion should not define:

- final prompt wording
- prompt examples
- few-shot examples
- model settings
- implementation details

Those belong to the implementation phase.

---

## Design goal

> Redesign the AI prompts so they align with the new architecture, with each prompt supporting a single, clearly defined stage of the pipeline. The prompts should reinforce the architectural separation of responsibilities rather than attempting to solve multiple problems within a single prompt.
> 

---

# Layman's summary

**What actually changes?**

- Rewrite the existing prompts to match the new architecture.
- Give each prompt **one clear job** instead of trying to solve everything at once.
- Discovery and Evaluation will each have their own dedicated prompt.
- The prompts become simpler because the architecture has already separated the responsibilities.
- Prompt improvements should follow the architecture, not redefine it.

---

I actually think this is the perfect final point because it closes the redesign.

Notice the order you've ended up with:

1. Present the transcript properly.
2. Redesign Discovery.
3. Return candidate objects.
4. Process candidates.
5. Evaluate candidates.
6. Remove arbitrary scoring.
7. Rewrite the prompts to match.

That's a coherent architectural progression. Each point builds on the previous one, and the final point is simply updating the prompts to reflect the new design rather than trying to invent a new architecture inside the prompts.


I actually think this is a good point to stop and zoom out.

One thing I like is that the pipeline is still recognisably your MK1 system. We haven't invented a completely different product—we've just untangled the responsibilities.

This is how I now see the finished architecture.

```
                Video
                   │
                   ▼
              WhisperX
                   │
                   ▼
      Transcript Presentation
        (Fixed partitions +
             overlap)
                   │
                   ▼
         Discovery Stage
  (Find every potential candidate)
                   │
                   ▼
         Candidate Objects
                   │
                   ▼
      Candidate Processing
      ├── Merge overlaps
      ├── Deduplicate
      ├── Future enrichers
      │     ├── Emotion
      │     ├── Face
      │     ├── Audio
      │     ├── ...
      ▼
      Enriched Candidates
                   │
                   ▼
        Evaluation Stage
 (Choose which candidates become clips)
                   │
                   ▼
          Final Clip List
                   │
                   ▼
             Rendering
                   │
                   ▼
            Post Processing
        (Captions, reframing,
          silence removal...)
                   │
                   ▼
              Output
```

---

## The responsibility of each stage

### 1. Transcript Presentation

**Question it answers**

> How do we present the transcript to Discovery?
> 

Nothing more.

---

### 2. Discovery

**Question it answers**

> What parts of this transcript have genuine clip potential?
> 

It doesn't care whether they'll actually be rendered.

---

### 3. Candidate Objects

This is simply the handoff.

Everything from here onwards works on the same candidate.

---

### 4. Candidate Processing

This is now the home for every candidate-level processor.

Initially:

```
Merge

↓

Deduplicate
```

Eventually:

```
Merge

↓

Deduplicate

↓

Emotion

↓

Face

↓

Audio

↓

...
```

without another redesign.

---

### 5. Evaluation

Question:

> Out of everything we now know about these candidates, which ones should become clips?
> 

Notice Evaluation doesn't discover.

It doesn't enrich.

It just decides.

---

### 6. Rendering

Only now does the system commit to producing clips.

---

### 7. Post Processing

Exactly what you've already been building:

- captions
- reframing
- silence removal
- etc.

Nothing changes here.

---

# What I like most

The responsibilities are now incredibly clean.

| Stage | Responsibility |
| --- | --- |
| Transcript Presentation | Present transcript effectively |
| Discovery | Find candidate moments |
| Candidate Processing | Improve understanding of candidates |
| Evaluation | Decide which candidates survive |
| Rendering | Produce the clips |
| Post Processing | Improve finished clips |

Every stage has exactly one job.

---

# What we actually changed

Notice how little of the system changed.

We did **not** redesign:

- WhisperX
- Rendering
- Post-processing
- Uploading
- Operations UI
- Scheduling

We only redesigned the **selection architecture**.

That's why I think this stays within MK1 scope.

---

# The biggest conceptual change

If I had to summarise the redesign in one sentence, it would be:

> **The system no longer tries to find the best clips in one step. It progressively narrows a pool of candidate clips until only the best remain.**
> 

I actually think that's a much more robust way of thinking about the problem.

Instead of:

```
Transcript

↓

Best clips
```

you're now doing:

```
Transcript

↓

Many candidate clips

↓

Better understood candidates

↓

Best candidates

↓

Rendered clips
```

That, to me, is the biggest architectural improvement you've designed over the last few days. It also gives you clear extension points: if you later discover a useful analysis technique (emotion, OCR, scene changes, etc.), it plugs into **Candidate Processing** without forcing you to rethink the rest of the pipeline.
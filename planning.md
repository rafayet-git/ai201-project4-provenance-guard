# Provenance Guard

Provenance Guard is a backend service that a creative-sharing platform can plug in to classify submitted text as human-written or AI-generated, score its confidence honestly, surface a plain-language transparency label, and let creators appeal a classification they disagree with. Every decision and appeal is recorded in a structured audit log.

A guiding principle runs through every design decision below: **on a writing platform, a false positive (calling a human's work AI) is the costly error.** It damages a real creator's reputation and trust. So the system is deliberately biased toward "uncertain" rather than confidently accusing a human, and the bar to declare content AI is higher than the bar to declare it human.

## Detection signals

The pipeline uses two independent signals — one semantic, one structural. They measure different properties of the text, so agreement between them is meaningful evidence and disagreement is a reason to back off to "uncertain."

### Signal 1 — LLM classification (Groq, `llama-3.3-70b-versatile`)

- **What it measures:** holistic semantic and stylistic coherence. We give the model the text and ask it to estimate the probability that the text was AI generated, plus a one-line rationale.
- **Why it differs human vs. AI:** AI prose tends to be tonally even, generic in imagery, and "safe"; human writing carries idiosyncrasy, voice, surprising word choice, and the occasional rough edge. A capable LLM picks up on these high-level patterns the way a careful reader would.
- **Output shape:** `{ "p_ai": float in [0,1], "rationale": str }`.
- **Blind spot:** an LLM detector is itself a probabilistic guess and is known to be biased against non-native English writers and very formal/clean prose (flagging them as AI). It can also be fooled by AI text that was lightly edited by a human. It is *not* ground truth — which is exactly why we never let it decide alone.

### Signal 2 — Stylometric heuristics (pure Python)

- **What it measures:** measurable statistical structure of the text:
  - **Sentence-length variance / burstiness** — the spread of sentence lengths.
  - **Type-token ratio (TTR)** — vocabulary diversity (unique words ÷ total).
  - **Punctuation density** — punctuation marks per word.
  - **Mean sentence length** — a proxy for syntactic complexity.
- **Why it differs human vs. AI:** AI text is statistically *uniform* — sentences cluster around a similar length, vocabulary is moderate and even, punctuation is regular. Human writing is *bursty*: short punchy sentences next to long winding ones, wider vocabulary swings, irregular punctuation. So **low variance + moderate everything ⇒ more AI-like; high variance ⇒ more human-like.**
- **Output shape:** `{ "p_ai": float in [0,1], "features": {...raw metrics...} }`.
- **Blind spot:** it's blind to *meaning*. A human poem built on heavy repetition and a tiny vocabulary looks statistically "uniform" and can be misread as AI. Very short texts (a couplet, a one-line bio) don't have enough sentences to measure variance at all, so the metrics are unreliable.

The two signals are independent because one reads *what the text says and how it reads*, the other reads *the numbers behind the text*. A blind spot of one is generally not a blind spot of the other.


## Combining signals → calibrated confidence

Each signal returns a probability that the text is AI, `p_ai ∈ [0,1]`.

```
combined_p_ai = 0.65 * llm_p_ai + 0.35 * stylometry_p_ai
```

We weight the LLM higher (0.65) because holistic reading is the stronger signal; stylometry (0.35) is a cheaper, structural sanity check that can catch or temper the LLM.

**Agreement-aware confidence.** Raw probability isn't the whole story — *how much
the two independent signals agree* is itself evidence. We compute:

```
disagreement = | llm_p_ai - stylometry_p_ai |        # 0 = perfect agreement
agreement     = 1 - disagreement                      # 1 = perfect agreement
base_certainty = max(combined_p_ai, 1 - combined_p_ai)  # 0.5 .. 1.0
confidence     = 0.5 + (base_certainty - 0.5) * agreement
```

So confidence lives in **[0.5, 1.0]** and is *pulled toward 0.5 when the signals disagree*. Two signals that both strongly say "AI" yield high confidence; one says AI and one says human yields low confidence even if the average looks decisive. This is what makes the score meaningful rather than cosmetic.

**Graceful degradation.** If the Groq call fails (network/quota/parse error), the pipeline falls back to stylometry alone, marks the LLM signal `unavailable`, and caps confidence at 0.65 so a single structural signal can never produce a "high-confidence" verdict.

### What a confidence number means to a user
- **0.50** — a coin flip. The system has no real idea. → always "uncertain".
- **~0.65** — a lean, not a conclusion. Still shown cautiously.
- **0.90+** — both signals strongly agree. Safe to state plainly.

### Verdict thresholds (asymmetric, on purpose)

```
if confidence < 0.62:              verdict = "uncertain"
elif combined_p_ai >= 0.72:        verdict = "ai"        # high bar to accuse
elif combined_p_ai <= 0.38:        verdict = "human"     # lower bar to clear
else:                              verdict = "uncertain"
```

The AI threshold (`p_ai ≥ 0.72`) is stricter than the human threshold (`p_ai ≤ 0.38`, i.e. `p_human ≥ 0.62`). Borderline content lands in "uncertain" instead of being labeled AI. **This is where the false-positive asymmetry lives.**

---

## Transparency label (three variants)

The label is what a *reader* on the platform sees. It must be plain language and make the confidence meaningful to a non-technical person. Confidence is shown as a word ("strong"/"moderate") and a percentage, never as a bare decimal.

**High-confidence AI** (`verdict = ai`):
> 🤖 **Likely AI-generated.** Our analysis suggests this piece was probably created with the help of generative AI (strong confidence, 91%). This is an automated estimate, not a certainty, and the creator can appeal this decision.

**High-confidence human** (`verdict = human`):
> ✍️ **Likely human-written.** Our analysis found no strong signs of AI generation in this piece (strong confidence, 88%). This is an automated estimate, not a guarantee of authorship.

**Uncertain** (`verdict = uncertain`):
> 🔍 **Not enough signal to call.** Our analysis couldn't confidently determine whether this piece was written by a human or generated by AI (low confidence, 57%). We're showing this honestly rather than guessing. No attribution claim is being made.

## Appeals workflow

- **Who can appeal:** the creator of a submitted piece, identified by its `content_id`.
- **What they provide:** the `content_id` and a free-text `reason` explaining why they believe the classification is wrong.
- **What the system does on receipt:**
  1. Looks up the original submission and its decision.
  2. Updates the content's `status` from `classified` → `under_review`.
  3. Writes an `appeal` entry to the audit log, linked to the `content_id`, capturing the creator's reasoning *alongside the original verdict, confidence, and signals used*.
  4. Returns confirmation with the new status.
- **Automated re-classification is intentionally not done** — appeals route to a human. The `under_review` status is what a reviewer's queue filters on.
- **What a reviewer sees** (`GET /log` / a future queue view): the original text excerpt, the verdict + confidence, both signal scores and the LLM rationale, and the creator's appeal reason — everything needed to make a human judgment.

## Anticipated edge cases

1. **Repetitive, small-vocabulary poetry.** A villanelle or a chant-like poem repeats lines and uses simple words. Stylometry sees low variance + low TTR and leans "AI." Mitigation: the LLM signal reads it as creative human work and disagrees, which lowers confidence and pushes the verdict to "uncertain" rather than a false AI accusation.
2. **Very short submissions (a couplet, a 20-word bio).** Too few sentences for variance/burstiness to mean anything; stylometry is noise. Mitigation: when the text is below a minimum length, stylometry's weight effectively yields a near-0.5 (no-signal) score and the verdict tends to "uncertain."
3. **Human-edited AI text (hybrid authorship).** Text that was AI-drafted then polished by a human sits genuinely between classes. The system is designed to land these in "uncertain" rather than force a side — which is the honestanswer.
4. **Non-native English / very formal prose.** Known to be over-flagged as AI by LLM detectors. The asymmetric thresholds and agreement-pulling confidence reduce (don't eliminate) the chance of a false AI label here; the appeal path is the backstop.

## Architecture

```
  SUBMISSION FLOW
  ===============

  client
    │  POST /submit  { text }
    ▼
  ┌─────────────┐  raw text   ┌────────────────────┐
  │  Flask      │────────────▶│ Signal 1: Groq LLM │── p_ai (semantic) ┐
  │  /submit    │             └────────────────────┘                   │
  │ (rate-      │  raw text   ┌────────────────────┐                   ▼
  │  limited)   │────────────▶│ Signal 2:          │── p_ai      ┌───────────────┐
  │             │             │ Stylometry         │  (structural)│  Scoring      │
  │             │             └────────────────────┘─────────────▶│  combine +    │
  │             │                                                  │  agreement →  │
  │             │                                  combined_p_ai,  │  confidence + │
  │             │◀─────────────────────────────────verdict────────│  verdict      │
  │             │                                                  └───────────────┘
  │             │  verdict+confidence ┌──────────────────┐
  │             │────────────────────▶│ Label generator  │── label text ┐
  │             │                     └──────────────────┘              │
  │             │  full decision      ┌──────────────────┐              │
  │             │────────────────────▶│ Audit log (SQLite)│             │
  │             │                     │ + submissions tbl │             │
  │             │                     └──────────────────┘              │
  │             │◀───────────────────────────────────────────────────── ┘
  └─────────────┘
    │  200 { content_id, attribution, confidence, label, signals }
    ▼
  client


  APPEAL FLOW
  ===========

  client
    │  POST /appeal  { content_id, reason }
    ▼
  ┌─────────────┐  content_id    ┌──────────────────┐
  │  Flask      │───────────────▶│ submissions tbl  │  status: classified
  │  /appeal    │                │ status →         │      → under_review
  │             │                │ "under_review"   │
  │             │                └──────────────────┘
  │             │  appeal + original decision  ┌──────────────────┐
  │             │─────────────────────────────▶│ Audit log (SQLite)│  event="appeal"
  │             │                              └──────────────────┘
  └─────────────┘
    │  200 { content_id, status: "under_review" }
    ▼
  client
```

**Narrative.** *Submission:* `POST /submit` passes raw text to both signals inmthe detection pipeline; each returns a `p_ai`. The scoring layer combines themm(0.65/0.35), derives an agreement-aware confidence, and picks one of three verdicts. The label generator turns that into reader-facing text, the whole decision is written to the SQLite audit log, and the structured response is returned. *Appeal:* `POST /appeal` looks up the content by `content_id`, flips its status to `under_review`, logs the appeal next to the original decision for a human reviewer, and confirms.

---

## AI Tool Plan

**M3 — submission endpoint + first signal.**
- *Spec I will provide to Claude:* §1 Detection signals (Signal 2 stylometry) + the Architecture diagram.
- *Ask:* Flask app skeleton with `POST /submit`, plus the pure-Python stylometry function returning `{p_ai, features}`.
- *Verify:* call the stylometry function directly on a clearly-uniform sample and a clearly-bursty sample and confirm the scores move in the expected direction before wiring it into the endpoint.

**M4 — second signal + confidence scoring.**
- *Spec I will provide to Claude:* §1 (Signal 1 LLM) + §2 Combining/uncertainty + diagram.
- *Ask:* the Groq classification function returning `{p_ai, rationale}`, plus the scoring function (combine, agreement-aware confidence, asymmetric thresholds).
- *Check:* run clearly-AI vs. clearly-human texts and confirm confidence is high at the extremes and low for borderline/disagreeing inputs.

**M5 — production layer.**
- *Spec I will provide to Claude:* §3 label variants + §4 appeals workflow + diagram.
- *Ask:* label-generation logic (three variants with strength words + percentage), the `POST /appeal` endpoint, SQLite audit log, and Flask-Limiter rate limits.
- *Verify:* drive inputs that reach all three label variants, confirm an appeal flips status to `under_review` and is logged, and confirm `GET /log` shows the decision and appeal entries.
```

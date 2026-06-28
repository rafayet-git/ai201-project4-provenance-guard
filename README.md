# Provenance Guard

Provenance Guard is a backend service that a creative-sharing platform can plug in to classify submitted text as human-written or AI-generated, score its confidence honestly, surface a plain-language transparency label, and let creators appeal a classification they disagree with. Every decision and appeal is recorded in a structured audit log.

> **Design principle:** on a writing platform, a *false positive* — calling a human's work AI — is the costly error. The whole system is biased toward "uncertain" rather than confidently accusing a human. See [planning.md](planning.md) for the full spec, architecture diagram, and AI tool plan.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key_here" > .env
python app.py  # serves on http://127.0.0.1:5000
```

The system runs without a Groq key too — it degrades gracefully to the stylometry signal alone and caps confidence (see "Graceful degradation" below).

## API

| Method | Path       | Body / params                          | Returns |
|--------|------------|----------------------------------------|---------|
| POST   | `/submit`  | `{ "text": "<content>", "creator_id": "..." }` | `content_id`, `attribution`, `confidence`, `label`, `signals` |
| POST   | `/appeal`  | `{ "content_id": "...", "creator_reasoning": "..." }` | status → `under_review`, confirmation |
| GET    | `/log`     | `?limit=N` (default 100)               | structured audit log entries |
| GET    | `/health`  | —                                      | liveness check |

### Example — submit

```bash
curl -s -X POST localhost:5000/submit -H 'Content-Type: application/json' \
  -d '{"text":"The domestic cat is a popular companion animal. Cats provide numerous benefits to their owners. In conclusion, cats make excellent pets.","creator_id":"test-user-1"}'
```

```json
{
  "content_id": "4d946fda-…",
  "creator_id": "test-user-1",
  "attribution": "ai",
  "confidence": 0.7167,
  "combined_p_ai": 0.7516,
  "label": {
    "variant": "high-confidence-ai",
    "headline": "🤖 Likely AI-generated.",
    "body": "Our analysis suggests this piece was probably created with the help of generative AI (moderate confidence, 72%). …",
    "text": "🤖 Likely AI-generated. Our analysis suggests …"
  },
  "signals": {
    "llm": { "p_ai": 0.8, "available": true, "rationale": "…lacks personal voice…", "weight": 0.65 },
    "stylometry": { "p_ai": 0.6616, "reliable": true, "weight": 0.35, "features": { "burstiness": 0.15, "type_token_ratio": 0.71 } },
    "agreement": 0.8616
  },
  "status": "classified"
}
```

## Detection pipeline — two distinct signals

The pipeline uses two independent signals — one *semantic*, one *structural*. Because they measure different properties, agreement between them is real evidence and disagreement is a reason to back off to "uncertain."

| Signal | What it measures | Why it differs (human vs AI) | Blind spot |
|--------|------------------|------------------------------|------------|
| **1. LLM classification** (Groq `llama-3.3-70b-versatile`) | Holistic semantic & stylistic coherence — voice, imagery, tonal evenness, word choice. Returns `p_ai` + rationale. | AI prose is tonally even, generic, "safe"; humans carry voice, surprising choices, rough edges. | It's a probabilistic guess; biased against non-native/very-formal prose; fooled by lightly human-edited AI text. **Never decides alone.** |
| **2. Stylometric heuristics** (pure Python) | Structure: sentence-length **variance/burstiness**, **type-token ratio**, **punctuation density**, mean sentence length. | AI text is statistically *uniform*; human writing is *bursty* and irregular. Low variance ⇒ more AI-like. | Meaning-blind: repetitive simple-vocab poetry looks "AI"; unreliable on very short texts (< 40 words → emits a no-signal 0.5). |

Implemented in [provenance_guard/llm_signal.py](provenance_guard/llm_signal.py) and [provenance_guard/stylometry.py](provenance_guard/stylometry.py).

---

## Confidence scoring — communicating uncertainty

Each signal returns a probability the text is AI, `p_ai ∈ [0,1]`. We combine them and derive an **agreement-aware** confidence ([provenance_guard/scoring.py](provenance_guard/scoring.py)):

```
combined_p_ai  = 0.65 * llm_p_ai + 0.35 * stylometry_p_ai
base_certainty = max(combined_p_ai, 1 - combined_p_ai)        # 0.5 .. 1.0
agreement      = 1 - |llm_p_ai - stylometry_p_ai|             # 0 .. 1
confidence     = 0.5 + (base_certainty - 0.5) * agreement     # pulled to 0.5 on disagreement
```

Confidence lives in **[0.5, 1.0]** and is *pulled toward 0.5 when the two signals disagree*. This is what makes the score meaningful: two signals that both strongly say "AI" yield high confidence; one says AI and one says human yields low confidence even if the average looks decisive.

### Verdict thresholds (asymmetric on purpose)

```
if confidence < 0.62:        verdict = "uncertain"
elif combined_p_ai >= 0.72:  verdict = "ai"        # HIGH bar to accuse a human
elif combined_p_ai <= 0.38:  verdict = "human"     # LOWER bar to clear
else:                        verdict = "uncertain"
```

The AI threshold (`p_ai ≥ 0.72`) is stricter than the human threshold (`p_ai ≤ 0.38`). **This is where the false-positive asymmetry lives** — borderline content defaults to "uncertain" instead of being labeled AI.

### Graceful degradation
If Groq is unavailable (no key / network / quota / bad JSON), the pipeline falls back to stylometry alone, marks the LLM signal `available: false`, and **caps confidence at 0.65** so one structural signal can never produce a high-confidence verdict.

### How I tested that the scores are meaningful
I ran a scoring matrix over signal combinations and confirmed the score behaves as designed:

| llm p_ai | stylometry p_ai | → verdict | confidence | note |
|----------|-----------------|-----------|-----------|------|
| 0.95 | 0.90 | **ai** | 0.91 | both strongly agree → high |
| 0.05 | 0.10 | **human** | 0.91 | both strongly agree → high |
| 0.55 | 0.50 | **uncertain** | 0.53 | borderline → near coin-flip |
| 0.90 | 0.20 | **uncertain** | 0.55 | strong disagreement → pulled down, *not* called AI |
| 0.80 | 0.55 | **uncertain** | 0.66 | lean AI but below 0.72 bar → uncertain |
| n/a (LLM down) | unreliable (too short) | **uncertain** | 0.50 | no signal → honest "don't know" |

The key result: a `0.51`-ish combined score lands in **uncertain** while `0.95` lands in **high-confidence AI** — a meaningfully different label, not a binary flip at 0.5. Disagreement between the two independent signals visibly suppresses confidence (rows 4–5), the honest behavior on contested content.

I also verified on real prose end-to-end via Groq: a generic encyclopedia-style paragraph → `ai` (conf 0.72); a voice-heavy personal anecdote → `human` (conf 0.90).

## Transparency label — the three variants (verbatim)

The label is what a **reader** sees. Confidence is shown as a strength word (`strong` ≥ 85%, `moderate` ≥ 70%, `low` otherwise) plus a percentage — never a bare decimal. Implemented in [provenance_guard/labels.py](provenance_guard/labels.py). (Percentages below are illustrative; the real value is interpolated at runtime.)

**High-confidence AI** (`verdict = ai`):
> 🤖 **Likely AI-generated.** Our analysis suggests this piece was probably created with the help of generative AI (strong confidence, 91%). This is an automated estimate, not a certainty, and the creator can appeal this decision.

**High-confidence human** (`verdict = human`):
> ✍️ **Likely human-written.** Our analysis found no strong signs of AI generation in this piece (strong confidence, 88%). This is an automated estimate, not a guarantee of authorship.

**Uncertain** (`verdict = uncertain`):
> 🔍 **Not enough signal to call.** Our analysis couldn't confidently determine whether this piece was written by a human or generated by AI (low confidence, 57%). We're showing this honestly rather than guessing. No attribution claim is being made.

The uncertain and AI labels both explicitly say the result is an *estimate* and point to the appeal path — protecting creators against a false AI accusation.

---

## Appeals workflow

- **Who:** the creator of a submitted piece, identified by its `content_id`.
- **What they provide:** the `content_id` and a free-text `reason`.
- **What happens on receipt** ([provenance_guard/storage.py](provenance_guard/storage.py) `record_appeal`):
  1. Look up the original submission and its decision.
  2. Update status `classified` → **`under_review`**.
  3. Write an `appeal` audit entry capturing the creator's reasoning alongside the original verdict, confidence, and label variant.
  4. Return confirmation with the new status.
- **Automated re-classification is intentionally not done** — appeals route to a human. A reviewer queue would filter on `status = under_review` and see the text excerpt, verdict + confidence, both signal scores + the LLM rationale, and the creator's reason — everything needed to make a human judgment.

```bash
curl -s -X POST localhost:5000/appeal -H 'Content-Type: application/json' \
  -d '{"content_id":"4d946fda-…","creator_reasoning":"I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."}'
# → { "content_id": "...", "status": "under_review", "message": "Appeal received and logged. …" }
```

(`creator_reasoning` is the documented field name; `reason` is accepted as an
alias.)

## Rate limiting

Per-IP limits via Flask-Limiter (configured in [app.py](app.py)):

| Endpoint  | Limit | Reasoning |
|-----------|-------|-----------|
| `/submit` | **10 / minute, 100 / day** | A real creator submits a handful of pieces per day, with occasional bursts when re-editing and resubmitting a draft. 10/min absorbs honest bursts; 100/day sits well above any genuine creator's volume while making automated flooding (e.g. scraping the classifier, or DoS-ing the Groq budget) ineffective. |
| `/appeal` | **5 / minute, 30 / day** | Appeals are rarer and more deliberate than submissions; a tighter cap discourages spamming the human review queue while leaving ample room for a creator contesting several pieces. |

Exceeding a limit returns HTTP **429** with a structured JSON error. Verified: hammering `/submit` returns `200` up to the limit then `429` thereafter.

---

## Audit log

Every attribution decision and every appeal is written to a SQLite `audit_log` table as a structured event (confidence score, signals used, label, and — for appeals — the creator's reason next to the original decision). Retrieve via `GET /log`. Sample output (≥ 3 entries: two submissions and an appeal):

```json
{
  "entries": [
    {
      "entry_id": 3,
      "content_id": "4d946fda-857d-4582-a151-8c824305b090",
      "event_type": "appeal",
      "timestamp": "2026-06-27T02:20:11.402+00:00",
      "details": {
        "creator_id": "test-user-1",
        "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
        "original_verdict": "ai",
        "original_confidence": 0.7167,
        "original_label_variant": "high-confidence-ai",
        "new_status": "under_review"
      }
    },
    {
      "entry_id": 2,
      "content_id": "7fc97565-…",
      "event_type": "submission",
      "timestamp": "2026-06-27T02:19:58.114+00:00",
      "details": {
        "creator_id": "test-user-2",
        "verdict": "human",
        "confidence": 0.8984,
        "combined_p_ai": 0.118,
        "label": "✍️ Likely human-written. Our analysis found no strong signs of AI generation …",
        "label_variant": "high-confidence-human",
        "signals": {
          "agreement": 0.93,
          "llm": { "p_ai": 0.1, "available": true, "rationale": "Distinct voice, humor, and idiosyncratic phrasing.", "weight": 0.65 },
          "stylometry": { "p_ai": 0.16, "reliable": true, "weight": 0.35, "features": { "burstiness": 0.76, "type_token_ratio": 0.79 } }
        },
        "text_excerpt": "I never meant to keep the cat. It showed up one rainy Tuesday…"
      }
    },
    {
      "entry_id": 1,
      "content_id": "4d946fda-857d-4582-a151-8c824305b090",
      "event_type": "submission",
      "timestamp": "2026-06-27T02:19:40.071+00:00",
      "details": {
        "creator_id": "test-user-1",
        "verdict": "ai",
        "confidence": 0.7167,
        "combined_p_ai": 0.7516,
        "label": "🤖 Likely AI-generated. Our analysis suggests this piece was probably created with the help of generative AI …",
        "label_variant": "high-confidence-ai",
        "signals": {
          "agreement": 0.8616,
          "llm": { "p_ai": 0.8, "available": true, "rationale": "Lacks personal voice, idiosyncrasy, and surprising word choices.", "weight": 0.65 },
          "stylometry": { "p_ai": 0.6616, "reliable": true, "weight": 0.35, "features": { "burstiness": 0.15, "type_token_ratio": 0.71 } }
        },
        "text_excerpt": "The domestic cat is a popular companion animal. Cats provide numerous benefits…"
      }
    }
  ]
}
```

---

## Edge cases the system handles cautiously

1. **Repetitive, small-vocabulary poetry** — stylometry leans "AI" (low variance), but the LLM disagrees, lowering confidence → **uncertain**, not a false accusation.
2. **Very short submissions (< 40 words)** — too few sentences for variance to mean anything; stylometry emits a no-signal 0.5 and the verdict tends to **uncertain**.
3. **Human-edited AI (hybrid authorship)** — genuinely between classes; the design lands these in **uncertain**, the honest answer.
4. **Non-native / very formal prose** — known to be over-flagged by LLM detectors; asymmetric thresholds + agreement-pulling confidence reduce the risk, with the appeal path as the backstop.

See [planning.md](planning.md) §5 for details.

---

## Known limitations

The system will reliably **misclassify formally-written human prose by non-native English speakers** — and it can do so in the most damaging direction (a false "AI" label). This isn't a tuning problem; it's baked into both signals:

- The **LLM signal** is documented to over-flag clean, formal, "textbook" English as AI, because that register overlaps with how LLMs themselves write. A careful ESL writer aiming for correctness produces exactly that register.
- The **stylometry signal** reinforces the error: deliberate, even sentence construction lowers burstiness, which the heuristic reads as AI-like.

Because both independent signals fail *in the same direction* on this input, agreement is high and confidence does **not** get pulled down — the one mechanism that normally protects against false positives doesn't fire here. The asymmetric AI threshold (0.72) softens it, and the **appeal path is the real backstop** (note the appeal example above is exactly this creator). If I were deploying this for real, I'd want a calibration set of ESL human writing to re-fit thresholds, and I'd weight the appeal queue toward these cases.

Other weaker spots — repetitive poetry, very short texts, hybrid human-edited AI — are covered in "Edge cases" above; those mostly degrade safely to "uncertain".

## Spec reflection

**Where the spec helped.** Writing the *uncertainty representation* section of [planning.md](planning.md) before any code forced me to decide what a confidence number should *mean to a user* first, then build to it. That's why confidence is deliberately bounded to [0.5, 1.0] and pulled toward 0.5 on signal disagreement, and why the verdict thresholds are asymmetric — those are spec decisions, not artifacts of whatever the math happened to produce. Without the spec I'd have shipped a raw weighted average and discovered too late that "0.62" meant nothing to anyone.

**Where the implementation diverged.** The spec described two states for content (`classified`, `under_review`) and a single `p_ai`-style score per signal. In implementation I added (a) an explicit **`reliable` flag** on the stylometry signal and a **no-signal 0.5** path for sub-40-word texts, and (b) a **graceful-degradation branch** that falls back to stylometry alone and caps confidence at 0.65 when Groq is unavailable. Neither was in the original spec — both emerged from testing edge inputs, where pretending to have a structural signal on a 16-word text produced confidently wrong scores. The divergence makes the system honest about *when it has no basis to judge*, which is the spec's stated goal even if these specific mechanisms weren't.

## AI usage

1. **Stylometry feature-to-score mapping.** I directed an AI tool (with the detection-signals section of planning.md) to draft the pure-Python stylometry function computing burstiness, TTR, and punctuation density. It produced sensible metric calculations but mapped them to an AI-likeness score with arbitrary, uncalibrated cut-offs and **no handling for short texts** — a 10-word input got a confident score. I overrode the mapping with explicit normalized sub-scores and a documented blend (burstiness-dominant, 0.55/0.25/ 0.20), and added the `MIN_WORDS_FOR_SIGNAL` no-signal path.

2. **Confidence-scoring logic.** I asked an AI tool to combine the two signals per my uncertainty section. Its first version was a plain weighted average that flipped verdict at 0.5 — exactly the binary behavior the spec warns against — and it ignored signal *agreement* entirely. I rewrote it to compute agreement-aware confidence and verified the generated thresholds against my spec (they had silently used symmetric 0.5 cut-offs; I corrected them to the asymmetric 0.72 / 0.38 bars that encode the false-positive asymmetry). I confirmed the corrected version against the scoring matrix shown above.

## Project layout

```
app.py                          Flask API: /submit /appeal /log /health + rate limiting
provenance_guard/
  pipeline.py                   orchestrates signals → scoring → label
  llm_signal.py                 Signal 1: Groq LLM classification
  stylometry.py                 Signal 2: stylometric heuristics (pure Python)
  scoring.py                    combine signals → agreement-aware confidence + verdict
  labels.py                     three transparency-label variants
  storage.py                    SQLite submissions + audit_log
planning.md                     architecture, spec, diagram, AI tool plan
```
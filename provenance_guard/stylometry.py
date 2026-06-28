"""Signal 2 — stylometric heuristics

Measures the *structure* of the text rather than its meaning. AI prose tends to
be statistically uniform (sentences cluster around one length, even vocabulary,
regular punctuation); human prose is bursty and irregular. We turn four metrics
into a single probability that the text is AI-generated.

Blind spots (see planning.md §5): meaning-blind, so repetitive simple-vocabulary
poetry can look "AI"; unreliable on very short texts with too few sentences.
"""

import re

# Below this many words, sentence-level variance is statistically meaningless,
# so we report a no-signal 0.5 instead of pretending to know.
MIN_WORDS_FOR_SIGNAL = 40


def _split_sentences(text):
    parts = re.split(r"[.!?]+", text)
    return [s.strip() for s in parts if s.strip()]


def _tokenize_words(text):
    return re.findall(r"[A-Za-z']+", text.lower())


def _clip01(x):
    return max(0.0, min(1.0, x))


def analyze(text):
    """Return {'p_ai': float in [0,1], 'features': {...}, 'reliable': bool}."""
    words = _tokenize_words(text)
    sentences = _split_sentences(text)
    word_count = len(words)

    # Not enough text to measure structure: emit a no-signal score.
    if word_count < MIN_WORDS_FOR_SIGNAL or len(sentences) < 2:
        return {
            "p_ai": 0.5,
            "reliable": False,
            "features": {
                "word_count": word_count,
                "sentence_count": len(sentences),
                "note": "text too short for reliable stylometry",
            },
        }

    sent_lengths = [len(_tokenize_words(s)) for s in sentences]
    mean_len = sum(sent_lengths) / len(sent_lengths)
    variance = sum((l - mean_len) ** 2 for l in sent_lengths) / len(sent_lengths)
    std_dev = variance ** 0.5
    # Coefficient of variation = burstiness, normalized for mean length.
    burstiness = std_dev / mean_len if mean_len else 0.0

    unique_words = len(set(words))
    ttr = unique_words / word_count  # type-token ratio (vocab diversity)

    punctuation = len(re.findall(r"[,;:\-—()\"'!?.]", text))
    punct_density = punctuation / word_count

    # --- Map each metric to an "AI-likeness" sub-score in [0,1] ---
    # Higher burstiness => more human => lower AI score.
    # Typical human burstiness ~0.5-0.8; AI ~0.2-0.4.
    burst_ai = _clip01((0.55 - burstiness) / 0.45)

    # Lower vocabulary diversity (for a given length) => slightly more AI-like.
    # TTR naturally falls with length, so this is a weak signal.
    ttr_ai = _clip01((0.62 - ttr) / 0.30)

    # Very regular punctuation density (~0.12-0.18 per word) reads AI-ish;
    # very high or very low density reads more human/idiosyncratic.
    punct_ai = _clip01(1.0 - abs(punct_density - 0.15) / 0.15)

    # Burstiness is the strongest structural tell, so it dominates the blend.
    p_ai = _clip01(0.55 * burst_ai + 0.25 * ttr_ai + 0.20 * punct_ai)

    return {
        "p_ai": round(p_ai, 4),
        "reliable": True,
        "features": {
            "word_count": word_count,
            "sentence_count": len(sentences),
            "mean_sentence_length": round(mean_len, 2),
            "sentence_length_std": round(std_dev, 2),
            "burstiness": round(burstiness, 4),
            "type_token_ratio": round(ttr, 4),
            "punctuation_density": round(punct_density, 4),
        },
    }

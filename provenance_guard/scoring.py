"""Combine the two signals into a calibrated, agreement-aware confidence and an
asymmetric three-way verdict. See planning.md §2.

The asymmetry is deliberate: it is harder to declare content "ai" (p_ai >= 0.72)
than "human" (p_ai <= 0.38). On a writing platform a false positive — calling a
human's work AI — is the costly error, so borderline cases default to
"uncertain".
"""

W_LLM = 0.65
W_STYLOMETRY = 0.35

# Verdict thresholds (asymmetric on purpose).
AI_THRESHOLD = 0.72        # p_ai must clear this to be called AI
HUMAN_THRESHOLD = 0.38     # p_ai must fall below this to be called human
MIN_CONFIDENCE = 0.62      # below this we never commit to a verdict

# A single structural signal can never produce a "high-confidence" verdict.
SINGLE_SIGNAL_CONFIDENCE_CAP = 0.65


def score(llm_result, stylometry_result):
    """Return a dict with combined_p_ai, confidence, verdict and the inputs."""
    llm_p = llm_result["p_ai"]
    sty_p = stylometry_result["p_ai"]
    llm_available = llm_result.get("available", False)
    sty_reliable = stylometry_result.get("reliable", False)

    if llm_available:
        combined = W_LLM * llm_p + W_STYLOMETRY * sty_p
    else:
        # Degrade gracefully: stylometry alone.
        combined = sty_p

    base_certainty = max(combined, 1.0 - combined)          # 0.5 .. 1.0
    disagreement = abs(llm_p - sty_p)
    agreement = 1.0 - disagreement                          # 0 .. 1

    if llm_available:
        # Pull confidence toward 0.5 when the two signals disagree.
        confidence = 0.5 + (base_certainty - 0.5) * agreement
    else:
        confidence = min(base_certainty, SINGLE_SIGNAL_CONFIDENCE_CAP)

    # If stylometry was unreliable (too short) and it's our only signal, we
    # genuinely don't know.
    if not llm_available and not sty_reliable:
        confidence = 0.5

    confidence = round(max(0.0, min(1.0, confidence)), 4)

    if confidence < MIN_CONFIDENCE:
        verdict = "uncertain"
    elif combined >= AI_THRESHOLD:
        verdict = "ai"
    elif combined <= HUMAN_THRESHOLD:
        verdict = "human"
    else:
        verdict = "uncertain"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "combined_p_ai": round(combined, 4),
        "signals": {
            "llm": {
                "p_ai": round(llm_p, 4),
                "available": llm_available,
                "rationale": llm_result.get("rationale", ""),
                "weight": W_LLM if llm_available else 0.0,
            },
            "stylometry": {
                "p_ai": round(sty_p, 4),
                "reliable": sty_reliable,
                "weight": W_STYLOMETRY if llm_available else 1.0,
                "features": stylometry_result.get("features", {}),
            },
            "agreement": round(agreement, 4),
        },
    }

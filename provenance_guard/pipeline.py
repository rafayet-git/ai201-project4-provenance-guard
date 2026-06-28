"""Orchestrates one submission through the full detection pipeline:
signal 1 (LLM) + signal 2 (stylometry) -> scoring -> label.
"""

from . import labels, llm_signal, scoring, stylometry


def analyze(text):
    """Run both signals, combine, and build the label. Returns (decision, label)."""
    llm_result = llm_signal.classify(text)
    sty_result = stylometry.analyze(text)
    decision = scoring.score(llm_result, sty_result)
    label = labels.build_label(decision["verdict"], decision["confidence"])
    return decision, label

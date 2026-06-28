"""Provenance Guard — Flask API.

Endpoints
  POST /submit  { "text": str }                 -> attribution + confidence + label
  POST /appeal  { "content_id": str, "reason": str } -> status -> under_review
  GET  /log     [?limit=N]                       -> structured audit log
  GET  /health                                   -> liveness check

Rate limiting (Flask-Limiter): see README for chosen limits and reasoning.
"""

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from provenance_guard import pipeline, storage

load_dotenv()

app = Flask(__name__)
storage.init_db()

# Rate limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

SUBMIT_LIMITS = "10 per minute;100 per day"
APPEAL_LIMITS = "5 per minute;30 per day"

MAX_TEXT_CHARS = 20000


@app.errorhandler(429)
def ratelimit_handler(e):
    return (
        jsonify(
            {
                "error": "rate_limit_exceeded",
                "message": "Too many requests. See the documented limits.",
                "detail": str(e.description),
            }
        ),
        429,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/submit", methods=["POST"])
@limiter.limit(SUBMIT_LIMITS)
def submit():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    creator_id = str(data.get("creator_id") or "anonymous").strip() or "anonymous"
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "invalid_input", "message": "'text' is required."}), 400
    if len(text) > MAX_TEXT_CHARS:
        return (
            jsonify(
                {
                    "error": "text_too_long",
                    "message": f"'text' exceeds {MAX_TEXT_CHARS} characters.",
                }
            ),
            400,
        )

    decision, label = pipeline.analyze(text)
    content_id = str(uuid.uuid4())
    storage.record_submission(content_id, creator_id, text, decision, label)

    return jsonify(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": decision["verdict"],          # ai | human | uncertain
            "confidence": decision["confidence"],
            "combined_p_ai": decision["combined_p_ai"],
            "label": label,
            "signals": decision["signals"],
            "status": "classified",
        }
    )


@app.route("/appeal", methods=["POST"])
@limiter.limit(APPEAL_LIMITS)
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id", "")
    # 'creator_reasoning' is the documented field; 'reason' accepted as an alias.
    reasoning = data.get("creator_reasoning") or data.get("reason") or ""
    if not content_id:
        return jsonify({"error": "invalid_input", "message": "'content_id' is required."}), 400
    if not isinstance(reasoning, str) or not reasoning.strip():
        return (
            jsonify({"error": "invalid_input", "message": "'creator_reasoning' is required."}),
            400,
        )

    updated = storage.record_appeal(content_id, reasoning.strip())
    if updated is None:
        return jsonify({"error": "not_found", "message": "Unknown content_id."}), 404

    return jsonify(
        {
            "content_id": content_id,
            "status": updated["status"],                 # under_review
            "message": "Appeal received and logged. The content is now under review.",
            "original_verdict": updated["verdict"],
            "original_confidence": updated["confidence"],
        }
    )


@app.route("/log", methods=["GET"])
def log():
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    return jsonify({"entries": storage.get_log(limit)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)

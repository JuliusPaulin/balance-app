"""The chat assistant endpoint.

Thin on purpose: the tools are in ``ai_tools``, the loop is in ``ai_chat``, and
this module only checks the request and reports what came back.
"""

from flask import Blueprint, request, jsonify

import config
from ai_backends import BackendUnavailable
from ai_chat import chat as run_chat, status as backend_status

bp = Blueprint("chat", __name__)

# The longest conversation the panel will carry. Each turn resends the whole
# history, so this is a cost ceiling as much as a context one.
MAX_TURNS = 20
MAX_MESSAGE_CHARS = 4000


@bp.route("/api/chat/status")
def chat_status():
    """Whether the assistant can answer right now, and if not, what is missing.

    Mirrors /api/import/bank/status: the feature announces its own absence
    rather than failing when someone tries to use it. For the local backend
    that means actually asking Ollama what it has — "not configured" is a
    useless thing to tell someone whose server is running and who simply typed
    the model name differently.
    """
    state = backend_status()
    return jsonify({
        "configured": config.ai_configured() and state["reachable"]
                      and state["model_installed"],
        **state,
    })


@bp.route("/api/chat", methods=["POST"])
def chat():
    """Answer one turn. Body: {"messages": [{"role", "content"}, ...]}."""
    if not config.ai_configured():
        return jsonify({"error": "The assistant is not configured",
                        "code": "not_configured"}), 400

    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages is required"}), 400
    if len(messages) > MAX_TURNS * 2:
        return jsonify({"error": "This conversation is too long — start a new one"}), 400

    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            return jsonify({"error": "Each message must be an object"}), 400
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            return jsonify({"error": "Each message needs a role and text content"}), 400
        if len(content) > MAX_MESSAGE_CHARS:
            return jsonify({"error": "That message is too long"}), 400
        cleaned.append({"role": role, "content": content})
    if cleaned[-1]["role"] != "user":
        return jsonify({"error": "The last message must be from the user"}), 400

    try:
        result = run_chat(cleaned)
    except BackendUnavailable as exc:
        # The model is the one moving part that is not a file on this disk, so
        # it is the one part that can simply be off. Say which.
        return jsonify({"error": str(exc), "code": "backend_unavailable"}), 503
    except Exception as exc:
        # The model is the one part of this app that is not on the user's own
        # disk, so it is the one part that can be down. Say which it was.
        from core import app as flask_app
        flask_app.logger.warning("chat failed: %s", exc)
        return jsonify({"error": "The assistant could not be reached",
                        "code": "upstream", "detail": str(exc)}), 502

    return jsonify(result)

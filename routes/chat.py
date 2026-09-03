"""The chat assistant endpoint.

Thin on purpose: the tools are in ``ai_tools``, the loop is in ``ai_chat``, and
this module only checks the request and reports what came back.
"""

import json
import queue
import threading

from flask import Blueprint, Response, jsonify, request, stream_with_context

import config
from ai.backends import BackendUnavailable
from ai.chat import chat as run_chat, status as backend_status

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
    # Asking whether it is ready is also when to make it so. The panel polls
    # this while it says "starting", and until now nothing was listening.
    if state.get("state") == "starting":
        from ai.runtime import nudge
        nudge()
    return jsonify({
        "configured": config.ai_configured() and state["reachable"]
                      and state["model_installed"],
        **state,
    })


@bp.route("/api/chat/download", methods=["POST"])
def chat_download():
    """Fetch the model. The one thing the panel can ask for on a first run.

    Returns at once and reports through `/api/chat/status`: 2.7 GB is minutes,
    not a request. Starting it twice is harmless — the download refuses to run
    beside itself — so this needs no guard of its own.
    """
    if config.AI_BACKEND != "bundled":
        return jsonify({"error": "This build does not download its own model",
                        "code": "not_bundled"}), 400
    from ai.runtime import start_download
    return jsonify(start_download())


def _read_conversation(body):
    """Validate the posted conversation. Returns ``(messages, error)``.

    Shared by both endpoints so the streaming one cannot drift into accepting
    something the plain one refuses. The check runs before either starts
    answering, so a bad request is still an ordinary 400 rather than an error
    delivered halfway down an event stream.
    """
    if not config.ai_configured():
        return None, ({"error": "The assistant is not configured",
                       "code": "not_configured"}, 400)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, ({"error": "messages is required"}, 400)
    if len(messages) > MAX_TURNS * 2:
        return None, ({"error": "This conversation is too long — start a new one"}, 400)

    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            return None, ({"error": "Each message must be an object"}, 400)
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            return None, ({"error": "Each message needs a role and text content"}, 400)
        if len(content) > MAX_MESSAGE_CHARS:
            return None, ({"error": "That message is too long"}, 400)
        cleaned.append({"role": role, "content": content})
    if cleaned[-1]["role"] != "user":
        return None, ({"error": "The last message must be from the user"}, 400)
    return cleaned, None


@bp.route("/api/chat", methods=["POST"])
def chat():
    """Answer one turn. Body: {"messages": [{"role", "content"}, ...]}."""
    cleaned, error = _read_conversation(request.get_json(silent=True) or {})
    if error:
        payload, status = error
        return jsonify(payload), status

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


def _sse(event):
    """One server-sent event. Compact, because tokens arrive one at a time."""
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


@bp.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """The same turn, watchable while it happens.

    An answer is two model calls and about five seconds, and until now all of
    it looked identical from outside: a dot animation, then everything at once.
    The events are the same facts the JSON endpoint returns, sent as they
    become true — which lookup is running, and the answer as it is written.

    The loop runs on a worker thread because it reports through a callback and
    this has to yield: a queue is the join between the two. Nothing in the loop
    needs the request context — ``current_user_id`` is a constant here and the
    tools open their own — so the thread is free to do the work.
    """
    cleaned, error = _read_conversation(request.get_json(silent=True) or {})
    if error:
        payload, status = error
        return jsonify(payload), status

    def generate():
        events = queue.Queue()
        outcome = {}

        def work():
            try:
                outcome["result"] = run_chat(cleaned, on_event=events.put)
            except BackendUnavailable as exc:
                outcome["error"] = {"error": str(exc), "code": "backend_unavailable"}
            except Exception as exc:
                from core import app as flask_app
                flask_app.logger.warning("chat stream failed: %s", exc)
                outcome["error"] = {"error": "The assistant could not be reached",
                                    "code": "upstream"}
            finally:
                events.put(None)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while True:
            event = events.get()
            if event is None:
                break
            yield _sse(event)
        worker.join()

        # The last event carries the whole answer, so the panel renders from
        # one authoritative payload rather than from what it managed to catch.
        yield _sse({"type": "error", **outcome["error"]} if "error" in outcome
                   else {"type": "done", **outcome["result"]})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

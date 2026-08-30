"""The Ollama backend's wire format.

These are the tests that would otherwise only fail on Julius's Mac. Everything
here fakes the HTTP layer and checks the shape of what we send and how we read
what comes back — the parts that are wrong silently rather than loudly.
"""

import json
from types import SimpleNamespace

import pytest
import requests

import ai_backends
from ai_backends import BackendUnavailable, OllamaBackend


def _reply(content="", tool_calls=None, prompt_tokens=120, eval_tokens=30):
    return {
        "message": {"role": "assistant", "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {})},
        "done": True,
        "prompt_eval_count": prompt_tokens,
        "eval_count": eval_tokens,
    }


class FakeResponse:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload)
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def posted(monkeypatch):
    """Capture what the backend POSTs; reply with whatever the test queues."""
    sent = []
    queue = []

    def _post(url, json=None, timeout=None):
        sent.append({"url": url, "body": json})
        return queue.pop(0) if queue else FakeResponse(_reply("ok"))

    monkeypatch.setattr(ai_backends.requests, "post", _post)
    return SimpleNamespace(sent=sent, queue=queue)


TOOLS = [{"name": "category_breakdown", "description": "Totals per category.",
          "input_schema": {"type": "object", "properties": {}, "required": []}}]


# ── What we send ──────────────────────────────────────────────────────────

def test_tools_are_sent_in_ollamas_function_shape(posted):
    OllamaBackend().complete("SYSTEM", [{"role": "user", "content": "hi"}], TOOLS)

    body = posted.sent[0]["body"]
    assert body["stream"] is False
    assert body["tools"] == [{
        "type": "function",
        "function": {"name": "category_breakdown", "description": "Totals per category.",
                     "parameters": {"type": "object", "properties": {}, "required": []}},
    }]


def test_the_system_prompt_leads_the_conversation(posted):
    OllamaBackend().complete("SYSTEM", [{"role": "user", "content": "hi"}], TOOLS)
    assert posted.sent[0]["body"]["messages"][0] == {"role": "system", "content": "SYSTEM"}


def test_temperature_is_low_because_this_is_routing_not_writing(posted):
    OllamaBackend().complete("S", [{"role": "user", "content": "hi"}])
    assert posted.sent[0]["body"]["options"]["temperature"] == pytest.approx(0.1)


def test_a_tool_result_goes_back_under_the_tool_role(posted):
    history = [
        {"role": "user", "content": "spend?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c0", "name": "category_breakdown", "input": {"month": "2026-05"}}]},
        {"role": "tool", "id": "c0", "name": "category_breakdown",
         "content": '{"total_eur": "61 €"}', "is_error": False},
    ]
    OllamaBackend().complete("S", history, TOOLS)

    wire = posted.sent[0]["body"]["messages"]
    assert wire[2]["tool_calls"] == [
        {"function": {"name": "category_breakdown", "arguments": {"month": "2026-05"}}}
    ]
    assert wire[3] == {"role": "tool", "tool_name": "category_breakdown",
                       "content": '{"total_eur": "61 €"}'}


def test_no_tools_key_when_they_are_withdrawn(posted):
    """The final round drops the tools so the turn ends in a sentence."""
    OllamaBackend().complete("S", [{"role": "user", "content": "hi"}], tools=None)
    assert "tools" not in posted.sent[0]["body"]


# ── What we read back ─────────────────────────────────────────────────────

def test_a_tool_call_is_read_out_of_the_reply(posted):
    posted.queue.append(FakeResponse(_reply(tool_calls=[
        {"function": {"name": "category_breakdown", "arguments": {"month": "2026-05"}}}])))

    turn = OllamaBackend().complete("S", [{"role": "user", "content": "hi"}], TOOLS)
    assert turn.tool_calls == [
        {"id": "call_0", "name": "category_breakdown", "input": {"month": "2026-05"}}]


def test_arguments_that_arrive_as_a_json_string_are_parsed(posted):
    """Some Ollama builds hand the arguments back as text, not an object."""
    posted.queue.append(FakeResponse(_reply(tool_calls=[
        {"function": {"name": "category_breakdown", "arguments": '{"month": "2026-05"}'}}])))

    turn = OllamaBackend().complete("S", [{"role": "user", "content": "hi"}], TOOLS)
    assert turn.tool_calls[0]["input"] == {"month": "2026-05"}


def test_unparseable_arguments_are_a_failed_call_not_a_crashed_turn(posted):
    posted.queue.append(FakeResponse(_reply(tool_calls=[
        {"function": {"name": "category_breakdown", "arguments": "{not json"}}])))

    turn = OllamaBackend().complete("S", [{"role": "user", "content": "hi"}], TOOLS)
    assert turn.tool_calls[0]["input"] == {}


def test_inline_reasoning_never_reaches_the_answer(posted):
    """A thinking model narrating itself is not an answer to show anyone."""
    posted.queue.append(FakeResponse(
        _reply("<think>Which tool? Probably breakdown.</think>You spent 61 €.")))

    turn = OllamaBackend().complete("S", [{"role": "user", "content": "hi"}])
    assert turn.text == "You spent 61 €."


def test_token_counts_come_back(posted):
    posted.queue.append(FakeResponse(_reply("hi", prompt_tokens=900, eval_tokens=40)))
    turn = OllamaBackend().complete("S", [{"role": "user", "content": "hi"}])
    assert turn.usage == {"input_tokens": 900, "output_tokens": 40}


# ── When it is not there ──────────────────────────────────────────────────

def test_a_server_that_is_not_running_says_so_in_words(monkeypatch):
    def _refused(*a, **k):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(ai_backends.requests, "post", _refused)

    with pytest.raises(BackendUnavailable, match="is it running"):
        OllamaBackend().complete("S", [{"role": "user", "content": "hi"}])


def test_a_model_without_a_thinking_mode_is_retried_without_it(monkeypatch):
    """The 400 this causes looks exactly like a bug in our own request."""
    calls = []

    def _post(url, json=None, timeout=None):
        calls.append(json)
        if "think" in json:
            return FakeResponse({}, status=400,
                                text="registry.ollama.ai does not support think")
        return FakeResponse(_reply("fine"))

    monkeypatch.setattr(ai_backends.requests, "post", _post)
    backend = OllamaBackend()
    assert backend.complete("S", [{"role": "user", "content": "hi"}]).text == "fine"

    # And it does not keep re-learning that on every later question.
    backend.complete("S", [{"role": "user", "content": "again"}])
    assert "think" not in calls[-1]


def test_status_names_the_model_to_pull_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(ai_backends.requests, "get",
                        lambda *a, **k: FakeResponse({"models": [{"name": "llama3:8b"}]}))

    state = OllamaBackend(model="qwen3.5:9b").status()
    assert state["reachable"] is True
    assert state["model_installed"] is False
    assert state["detail"] == "Run: ollama pull qwen3.5:9b"
    assert state["installed_models"] == ["llama3:8b"]


def test_status_is_happy_when_the_model_is_installed(monkeypatch):
    monkeypatch.setattr(ai_backends.requests, "get",
                        lambda *a, **k: FakeResponse({"models": [{"name": "qwen3.5:9b"}]}))
    assert OllamaBackend(model="qwen3.5:9b").available() is True


def test_a_bare_model_name_matches_its_latest_tag(monkeypatch):
    monkeypatch.setattr(ai_backends.requests, "get",
                        lambda *a, **k: FakeResponse({"models": [{"name": "mistral:latest"}]}))
    assert OllamaBackend(model="mistral").available() is True


def test_status_reports_a_dead_server_without_raising(monkeypatch):
    def _refused(*a, **k):
        raise requests.ConnectionError("nope")
    monkeypatch.setattr(ai_backends.requests, "get", _refused)

    state = OllamaBackend().status()
    assert state["reachable"] is False
    assert "No Ollama" in state["detail"]


# ── Picking one ───────────────────────────────────────────────────────────

def test_local_is_the_default_backend():
    import config
    assert config.AI_BACKEND == "local"
    assert ai_backends.get_backend().name == "local"


def test_an_unknown_backend_name_is_refused():
    with pytest.raises(BackendUnavailable, match="Unknown AI_BACKEND"):
        ai_backends.get_backend("telepathy")

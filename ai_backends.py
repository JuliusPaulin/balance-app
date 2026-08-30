"""Where the model actually runs. One interface, two implementations.

The assistant is meant to run on the user's own Mac (see
``docs/LOCAL_AI_RESEARCH.md``), so the provider is the part most likely to
change — first Ollama, later a llama.cpp embedded in the app itself. Everything
above this file works in one neutral vocabulary and never learns which model
answered.

A backend takes a system prompt, a neutral history and the tool schemas, and
returns a :class:`Turn`. Translating to and from the provider's wire format is
its entire job.

The neutral history is a list of:

    {"role": "user",      "content": str}
    {"role": "assistant", "content": str, "tool_calls": [...], "raw": <native>}
    {"role": "tool",      "id": str, "name": str, "content": str, "is_error": bool}

``raw`` lets a backend replay its own provider-native content verbatim rather
than have it rebuilt from the neutral form. That matters for reasoning blocks,
which some APIs require back byte-identical and which nothing here understands.
"""

import json
import re
from collections import namedtuple

import requests

import config

Turn = namedtuple("Turn", "text tool_calls refusal usage raw")
"""One reply. ``tool_calls`` is ``[{"id", "name", "input"}]`` — empty means the
model is done talking and ``text`` is the answer."""

# Some local models narrate their reasoning inline instead of in a separate
# field. It is not an answer and it must never reach the panel.
_THINK_TAGS = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class BackendUnavailable(RuntimeError):
    """The model could not be reached — distinct from the model refusing."""


# ── Ollama (the local backend, and the default) ───────────────────────────

class OllamaBackend:
    """Talks to a local Ollama server over its /api/chat endpoint.

    Nothing here leaves the machine. That is the whole point of the feature:
    the app has always been one SQLite file on your own disk, and a chat panel
    that posts a transaction history to somebody's API would be the first thing
    it ever did that contradicts that.
    """

    name = "local"

    def __init__(self, host=None, model=None, num_ctx=None, temperature=None):
        self.host = (host or config.OLLAMA_HOST).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.num_ctx = num_ctx if num_ctx is not None else config.OLLAMA_NUM_CTX
        self.temperature = (temperature if temperature is not None
                            else config.OLLAMA_TEMPERATURE)
        # Thinking is worth a round trip to discover rather than a config flag
        # to get wrong: some models reject the field outright, and the failure
        # is a 400 that looks like a bug in our request.
        self._send_think = True

    # -- availability ------------------------------------------------------

    def status(self):
        """What is actually installed, so a mismatch can say which one it is.

        "Not configured" is a useless thing to tell someone who has Ollama
        running and simply spelled the model name differently.
        """
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=2)
            response.raise_for_status()
            installed = [m["name"] for m in response.json().get("models", [])]
        except Exception as exc:
            return {"backend": self.name, "model": self.model, "reachable": False,
                    "model_installed": False, "installed_models": [],
                    "detail": f"No Ollama at {self.host} ({exc.__class__.__name__})"}

        # Ollama reports "qwen3.5:9b"; a bare "qwen3.5" means the same tag.
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        found = wanted in installed or self.model in installed
        return {
            "backend": self.name, "model": self.model, "reachable": True,
            "model_installed": found, "installed_models": installed,
            "detail": None if found else f"Run: ollama pull {self.model}",
        }

    def available(self):
        state = self.status()
        return bool(state["reachable"] and state["model_installed"])

    # -- wire format -------------------------------------------------------

    @staticmethod
    def _wire_messages(system, messages):
        wire = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "tool":
                # Ollama takes tool output as its own role. `tool_name` is what
                # newer builds use to tie it back to the call; older ones ignore
                # the extra key rather than failing.
                wire.append({"role": "tool", "tool_name": message["name"],
                             "content": message["content"]})
            elif role == "assistant":
                entry = {"role": "assistant", "content": message.get("content", "")}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"function": {"name": c["name"], "arguments": c["input"]}}
                        for c in message["tool_calls"]
                    ]
                wire.append(entry)
            else:
                wire.append({"role": "user", "content": message["content"]})
        return wire

    @staticmethod
    def _wire_tools(tools):
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in (tools or [])]

    def _post(self, payload):
        try:
            response = requests.post(f"{self.host}/api/chat", json=payload,
                                     timeout=config.OLLAMA_TIMEOUT)
        except requests.RequestException as exc:
            raise BackendUnavailable(
                f"No Ollama at {self.host} — is it running? ({exc})") from exc

        if response.status_code == 400 and "think" in response.text.lower() \
                and payload.get("think") is not None:
            # This model has no thinking mode. Drop the field and never send it
            # again for the life of this backend.
            self._send_think = False
            payload.pop("think", None)
            return self._post(payload)

        if not response.ok:
            raise BackendUnavailable(
                f"Ollama returned {response.status_code}: {response.text[:300]}")
        return response.json()

    # -- the call ----------------------------------------------------------

    def complete(self, system, messages, tools=None):
        payload = {
            "model": self.model,
            "messages": self._wire_messages(system, messages),
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = self._wire_tools(tools)
        if self._send_think:
            # Off by default. The reasoning a thinking model does before
            # picking one of six tools costs seconds in a side panel and buys
            # very little. OLLAMA_THINK=1 turns it back on.
            payload["think"] = config.OLLAMA_THINK

        data = self._post(payload)
        message = data.get("message") or {}
        text = _THINK_TAGS.sub("", message.get("content") or "").strip()

        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                # Some builds hand the arguments back as a JSON string. A tool
                # call we cannot parse is a failed call, not a crashed turn.
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            calls.append({"id": call.get("id") or f"call_{index}",
                          "name": function.get("name", ""),
                          "input": arguments or {}})

        return Turn(
            text=text, tool_calls=calls, refusal=None,
            usage={"input_tokens": data.get("prompt_eval_count", 0),
                   "output_tokens": data.get("eval_count", 0)},
            raw=None,
        )


# ── Anthropic (kept so the two can be compared) ───────────────────────────

class AnthropicBackend:
    """The cloud backend. Not the destination — the control.

    Worth keeping precisely because it is the only way to tell a bad answer
    caused by the model apart from one caused by the prompt or the tools:
    same loop, same tools, different brain.
    """

    name = "anthropic"

    def __init__(self, model=None):
        self.model = model or config.AI_MODEL

    def status(self):
        return {"backend": self.name, "model": self.model,
                "reachable": bool(config.ANTHROPIC_API_KEY),
                "model_installed": bool(config.ANTHROPIC_API_KEY),
                "installed_models": [],
                "detail": None if config.ANTHROPIC_API_KEY else "Set ANTHROPIC_API_KEY"}

    def available(self):
        return bool(config.ANTHROPIC_API_KEY)

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendUnavailable(
                "The anthropic package is not installed (pip install anthropic)") from exc
        return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)

    @staticmethod
    def _wire_messages(messages):
        """Neutral history → Anthropic messages.

        Assistant turns are replayed from ``raw`` so reasoning blocks go back
        exactly as they came, and consecutive tool results are gathered into a
        single user message — splitting them teaches the model to stop calling
        tools in parallel.
        """
        wire, pending = [], []

        def flush():
            if pending:
                wire.append({"role": "user", "content": list(pending)})
                pending.clear()

        for message in messages:
            if message["role"] == "tool":
                pending.append({"type": "tool_result",
                                "tool_use_id": message["id"],
                                "content": message["content"],
                                "is_error": message.get("is_error", False)})
                continue
            flush()
            if message["role"] == "assistant":
                wire.append({"role": "assistant",
                             "content": message.get("raw") or message.get("content", "")})
            else:
                wire.append({"role": "user", "content": message["content"]})
        flush()
        return wire

    def complete(self, system, messages, tools=None):
        client = self._client()
        request = {
            "model": self.model,
            "max_tokens": 4096,
            # The stable half of the prompt is cached; the per-conversation
            # context is appended after the breakpoint so it never invalidates it.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": self._wire_messages(messages),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": config.AI_EFFORT},
        }
        if tools:
            request["tools"] = [
                {"name": t["name"], "description": t["description"],
                 "input_schema": t["input_schema"]} for t in tools
            ]
        response = client.messages.create(**request)

        usage = {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens}
        if response.stop_reason == "refusal":
            return Turn(text="", tool_calls=[], usage=usage, raw=None,
                        refusal=getattr(response.stop_details, "explanation", None)
                        or "I can't answer that one.")

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [{"id": b.id, "name": b.name, "input": b.input}
                 for b in response.content if b.type == "tool_use"]
        return Turn(text=text, tool_calls=calls, refusal=None, usage=usage,
                    raw=response.content)


BACKENDS = {"local": OllamaBackend, "anthropic": AnthropicBackend}


def get_backend(name=None):
    """The configured backend. ``AI_BACKEND=local`` (the default) means Ollama."""
    name = (name or config.AI_BACKEND).lower()
    if name not in BACKENDS:
        raise BackendUnavailable(
            f"Unknown AI_BACKEND {name!r} — pick one of {', '.join(BACKENDS)}")
    return BACKENDS[name]()

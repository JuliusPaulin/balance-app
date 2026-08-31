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

    def _post_stream(self, payload, on_token):
        """Ollama's streaming reply: one JSON object per line.

        Returns the same shape ``_post`` does, assembled from the chunks, and
        hands each piece of answer text to ``on_token`` as it lands. That is the
        whole point — the first word arrives about a second in, where the whole
        answer takes five.
        """
        payload = {**payload, "stream": True}
        try:
            response = requests.post(f"{self.host}/api/chat", json=payload,
                                     stream=True, timeout=config.OLLAMA_TIMEOUT)
        except requests.RequestException as exc:
            raise BackendUnavailable(
                f"No Ollama at {self.host} — is it running? ({exc})") from exc

        if response.status_code == 400 and "think" in response.text.lower() \
                and payload.get("think") is not None:
            self._send_think = False
            payload.pop("think", None)
            return self._post_stream(payload, on_token)
        if not response.ok:
            raise BackendUnavailable(
                f"Ollama returned {response.status_code}: {response.text[:300]}")

        content, calls, final = [], [], {}
        # A model that narrates itself must not narrate itself into the panel.
        # `think` is off by default, so this is a net rather than a mechanism.
        thinking = False
        for line in response.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            message = chunk.get("message") or {}
            piece = message.get("content") or ""
            if piece:
                content.append(piece)
                joined = "".join(content)
                if "<think>" in joined and "</think>" not in joined:
                    thinking = True
                elif thinking and "</think>" in joined:
                    thinking = False
                    piece = ""
                if on_token and not thinking and piece:
                    on_token(piece)
            if message.get("tool_calls"):
                calls.extend(message["tool_calls"])
            if chunk.get("done"):
                final = chunk

        return {**final, "message": {"content": "".join(content),
                                     "tool_calls": calls}}

    def complete(self, system, messages, tools=None, on_token=None):
        payload = {
            "model": self.model,
            "messages": self._wire_messages(system, messages),
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
            # Keep the weights resident between questions. Ollama drops them
            # after five minutes, and reloading them is the slowest thing that
            # happens in this whole feature.
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
        }
        if tools:
            payload["tools"] = self._wire_tools(tools)
        if self._send_think:
            # Off by default. The reasoning a thinking model does before
            # picking one of six tools costs seconds in a side panel and buys
            # very little. OLLAMA_THINK=1 turns it back on.
            payload["think"] = config.OLLAMA_THINK

        data = self._post_stream(payload, on_token) if on_token else self._post(payload)
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

    def complete(self, system, messages, tools=None, on_token=None):
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
        # The control backend answers in one piece. The panel streams either
        # way; this one simply arrives all at once.
        if on_token and text:
            on_token(text)
        calls = [{"id": b.id, "name": b.name, "input": b.input}
                 for b in response.content if b.type == "tool_use"]
        return Turn(text=text, tool_calls=calls, refusal=None, usage=usage,
                    raw=response.content)


# ── llama.cpp, bundled with the app ───────────────────────────────────────

class LlamaCppBackend:
    """The one that ships inside Balance.app.

    Ollama is what this was built and proved against, and it stays: swapping
    models is one `ollama pull` rather than a rebuild, which is worth a lot
    while the prompt is still moving. But it is a separate thing to install,
    and nobody installs a second app to try a side panel. So the shipped
    default talks to a copy of llama.cpp's own server that travels in the
    bundle, over its OpenAI-shaped API, against one model file this app
    downloaded and owns.

    Everything the loop needs was checked before a line of this was written —
    the tool calls come back in the same shape, the prompt prefix is cached
    between the two calls of a turn, and the answer streams. One thing did not
    survive the move and would have gone unnoticed: Qwen thinks by default here,
    and llama.cpp's `--reasoning off` only decides where the thoughts are put,
    not whether they happen. `enable_thinking: false` through the chat template
    is the actual switch, and it is the difference between a first word at
    0.14s and one at 14.4s.
    """

    name = "llamacpp"

    def __init__(self, host=None, model_path=None, num_ctx=None, temperature=None):
        self.host = (host or config.LLAMACPP_HOST).rstrip("/")
        self.model_path = model_path or config.model_file()
        self.num_ctx = num_ctx if num_ctx is not None else config.OLLAMA_NUM_CTX
        self.temperature = (temperature if temperature is not None
                            else config.OLLAMA_TEMPERATURE)

    # -- readiness ---------------------------------------------------------

    def status(self):
        """Whether it can answer, and if not, which of the reasons it is.

        There are more of them than Ollama had. The weights are ours to fetch
        now, so "not ready" splits into never downloaded, downloading, and up
        but still loading — three different sentences and only one of them asks
        the user for anything.
        """
        from model_runtime import runtime_state
        return runtime_state(self.host, self.model_path)

    def available(self):
        return self.status()["configured"]

    # -- wire format -------------------------------------------------------

    @staticmethod
    def _wire_messages(system, messages):
        wire = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "tool":
                # OpenAI ties output back to the call by id, where Ollama used
                # the tool's name.
                wire.append({"role": "tool", "tool_call_id": message["id"],
                             "content": message["content"]})
            elif role == "assistant":
                entry = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["input"])}}
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

    @staticmethod
    def _read_calls(raw_calls):
        calls = []
        for index, call in enumerate(raw_calls or []):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                # This server always sends a JSON string, per OpenAI. One we
                # cannot parse is a failed call, not a crashed turn.
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            calls.append({"id": call.get("id") or f"call_{index}",
                          "name": function.get("name", ""),
                          "input": arguments or {}})
        return calls

    # -- the call ----------------------------------------------------------

    def _payload(self, system, messages, tools):
        return {
            "messages": self._wire_messages(system, messages),
            "temperature": self.temperature,
            # The switch that matters. Without it the model reasons its way to
            # every answer and the panel waits fourteen seconds for a word.
            "chat_template_kwargs": {"enable_thinking": config.OLLAMA_THINK},
            **({"tools": self._wire_tools(tools)} if tools else {}),
        }

    def complete(self, system, messages, tools=None, on_token=None):
        # Cheap when the server is already up — one call to /health — and the
        # difference between an answer and an error when it is not. The app
        # starts it at launch; this is for every case where that did not happen.
        from model_runtime import ensure_running
        if not ensure_running():
            raise BackendUnavailable(
                "Balance AI could not start. Its model may still be downloading.")
        url = f"{self.host}/v1/chat/completions"
        payload = self._payload(system, messages, tools)
        try:
            if on_token:
                return self._stream(url, payload, on_token)
            response = requests.post(url, json={**payload, "stream": False},
                                     timeout=config.OLLAMA_TIMEOUT)
        except requests.RequestException as exc:
            raise BackendUnavailable(f"Balance AI is not answering ({exc})") from exc
        if not response.ok:
            raise BackendUnavailable(
                f"Balance AI returned {response.status_code}: {response.text[:300]}")

        data = response.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        usage = data.get("usage") or {}
        return Turn(
            text=(message.get("content") or "").strip(),
            tool_calls=self._read_calls(message.get("tool_calls")),
            refusal=None,
            usage={"input_tokens": usage.get("prompt_tokens", 0),
                   "output_tokens": usage.get("completion_tokens", 0)},
            raw=None,
        )

    def _stream(self, url, payload, on_token):
        """Server-sent events, assembled back into one Turn.

        Tool calls arrive in pieces here too — a name in one chunk and its
        arguments across several — so they are stitched by index before being
        read.
        """
        content, usage, calls = [], {}, {}
        # Without asking, the usage totals never arrive on this path and every
        # turn reports zero tokens in and zero out — which is what the terminal
        # harness prints to tell a prompt problem from a model one.
        payload = {**payload, "stream_options": {"include_usage": True}}
        with requests.post(url, json={**payload, "stream": True}, stream=True,
                           timeout=config.OLLAMA_TIMEOUT) as response:
            if not response.ok:
                raise BackendUnavailable(
                    f"Balance AI returned {response.status_code}: "
                    f"{response.text[:300]}")
            for line in response.iter_lines():
                if not line or not line.startswith(b"data: "):
                    continue
                body = line[6:]
                if body.strip() == b"[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except ValueError:
                    continue
                usage = chunk.get("usage") or usage
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                # `reasoning_content` is the model thinking aloud. It is off,
                # and if it ever comes back on it is still not an answer.
                piece = delta.get("content")
                if piece:
                    content.append(piece)
                    on_token(piece)
                for part in delta.get("tool_calls") or []:
                    slot = calls.setdefault(part.get("index", 0),
                                            {"id": None, "name": "", "arguments": ""})
                    if part.get("id"):
                        slot["id"] = part["id"]
                    function = part.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        stitched = [{"id": c["id"], "function": {"name": c["name"],
                                                 "arguments": c["arguments"]}}
                    for _, c in sorted(calls.items())]
        return Turn(
            text="".join(content).strip(),
            tool_calls=self._read_calls(stitched),
            refusal=None,
            usage={"input_tokens": usage.get("prompt_tokens", 0),
                   "output_tokens": usage.get("completion_tokens", 0)},
            raw=None,
        )


BACKENDS = {"bundled": LlamaCppBackend,
            "local": OllamaBackend,
            "anthropic": AnthropicBackend}


def get_backend(name=None):
    """The configured backend.

    ``bundled`` is llama.cpp travelling inside the app and is what ships.
    ``local`` is Ollama, kept because swapping models is one `ollama pull`
    rather than a rebuild. ``anthropic`` is the control.
    """
    name = (name or config.AI_BACKEND).lower()
    if name not in BACKENDS:
        raise BackendUnavailable(
            f"Unknown AI_BACKEND {name!r} — pick one of {', '.join(BACKENDS)}")
    return BACKENDS[name]()

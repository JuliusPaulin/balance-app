"""The assistant: the model, the loop, and the seven things it may read.

Four modules, in the order a question passes through them:

``tools``     the seven read-only tools, each a wrapper over an endpoint the
              app already serves. The model picks one; it never writes SQL.
``chat``      the agent loop — the system prompt and the tool-call cycle.
``backends``  where the model runs: llama.cpp in the bundle, Ollama for
              development, Anthropic as the control, behind one interface.
``runtime``   the model that ships with the app — where the runtime and the
              weights live, fetching them once, and which of the GPU and the
              CPU it ended up on.

They import downwards only — ``chat`` reads ``backends`` and ``tools``,
``backends`` reads ``runtime``, and nothing reads back up.
"""

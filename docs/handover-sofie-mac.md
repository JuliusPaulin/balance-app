# Handover: Balance AI will not use the GPU on this Mac

You are on **Sofie's MacBook Air**, not the machine Balance is developed on.
There may be no checkout of the source here, and none is needed for the first
part. The source lives at
`https://github.com/JuliusPaulin/balance-app` if you come to want it.

**The one question:** Balance's assistant panel either never answers or answers
after minutes. On this Mac the model server keeps running on the CPU. It should
run on the GPU.

---

## What Balance is, in four lines

A personal finance app for one person: Flask and SQLite in a native window, all
on this Mac. Version 1.10 added an assistant panel that answers questions about
the figures. It runs a local model — `llama-server` (llama.cpp) inside the app
bundle, against a 2.7 GB Qwen 3.5 4B file it downloads once. Nothing leaves the
disk, and the assistant can only read.

## Where things are

| | |
|---|---|
| App | `/Applications/Balance.app` |
| Its window | `http://127.0.0.1:5050` |
| Model server | `http://127.0.0.1:5051` |
| Model file | `~/Library/Application Support/Balance/models/Qwen3.5-4B-Q4_K_M.gguf` |
| Server log | `~/Library/Application Support/Balance/models/server.log` |
| Her data | `~/Library/Application Support/Balance/expenses.db` |

## Rules for this machine

- **Read her data, never write it.** The assistant is read-only by design and
  the app has no undo for a hand-edited row. Do not touch `expenses.db`.
- **Do not update macOS.** She is on Ventura 13.5. The bundled runtime needs
  13.3, so this is fine, and it has been tested: her GPU works on this OS.
- **Do not install Ollama.** The whole point of the release is that a user
  installs nothing.
- Killing `llama-server` is safe. It holds no state — it reloads the model.

---

## What has already been ruled out, with evidence

Six versions were shipped at this bug. Do not re-test these:

| Theory | Ruled out by |
|---|---|
| The model file downloaded short | Byte-exact against the expected 2 740 937 888, and starts `GGUF` |
| macOS too old for the runtime | 13.5 is above the 13.3 the binary needs |
| Not enough memory | 16 GB |
| The context was cut too small | Was a real bug (a 4 096 context is under the ~5 100 token floor); fixed in 1.10.4 with `MIN_CTX = 8192` |
| **The GPU itself is broken on this Mac** | **No.** Running the bundled binary by hand with `--n-gpu-layers 999 --load-mode none` started on the GPU in seconds. She saw it print "THE GPU WORKS". |

That last row is the important one. **The hardware is fine and the flags are
right.** The question is only why the app's own server does not end up with
them.

## What the app does when it starts the server

Two ways to run, tried in order. The model is never memory-mapped at either —
memory-mapping is what makes Metal fail (`newBufferWithBytesNoCopy:` returns nil
for a pointer that is not page-aligned, and llama.cpp dies on
`GGML_ASSERT(buf_dst) failed`).

```
level 0   --ctx-size 8192 --load-mode none --n-gpu-layers 999   ← the GPU, wanted
level 1   --ctx-size 8192 --load-mode none --n-gpu-layers 0     ← the CPU, ~26 tok/s
```

Level 1 is unusable: about two minutes a question, and a month's analysis cannot
finish inside the timeout at all. **If she is on level 1, that is the bug.**

---

## The leading hypothesis — test this first

`llama-server` is a **child process** of Balance, and Balance only stops it when
its window closes cleanly. Force-quit the app — which happened many times while
this was being debugged — and the server stays resident with port 5051 still
answering.

Until 1.10.7, the app started by asking "is something already answering with our
model file open?" A leftover from an older version answers yes. **So the new app
adopts the old CPU-bound server and never starts its own.** Reinstalling cannot
fix that. Only killing it or a reboot can.

She is on **1.10.6**, which has the right flags but still adopts leftovers.
1.10.7 (written, committed, not yet released) stops the adoption.

### Run this

```bash
echo "── what is running now ──"; ps -Ao pid=,lstart=,args= | grep "[l]lama-server"; osascript -e 'quit app "Balance"' >/dev/null 2>&1; sleep 3; pkill -f llama-server; sleep 2; open -a Balance; echo "── waiting 40s ──"; sleep 40; ps -Ao pid=,args= | grep "[l]lama-server"
```

Then the full check, which also asks the assistant a real question:

```bash
curl -fsSL https://raw.githubusercontent.com/JuliusPaulin/balance-app/main/scripts/check_ai.sh | bash
```

### Reading the result

- **The first block is the diagnosis.** If the leftover server started *before*
  the last update, the hypothesis was right and this was never a GPU problem.
- **Wanted:** `On the GPU, without the memory map` and an answer in roughly
  15–25 seconds.
- **`ON THE CPU`:** the app started its own server and it still fell back. Go to
  the next section.

---

## If it is still on the CPU

Then level 0 really is failing here, and the log now holds both halves — it
appends rather than truncating, so the crash sits above the fallback.

```bash
tail -120 ~/Library/Application\ Support/Balance/models/server.log
```

Look for the `=== starting: … ===` headers. Each one is an attempt, in order.
What matters is **what the level 0 attempt said before the level 1 attempt
began**. Likely lines: `GGML_ASSERT`, `Library not loaded`, `Symbol not found`,
`built for newer`.

Then run the bundled binary by hand with exactly the app's flags, and compare —
this is the test that separates the binary from the way the app launches it:

```bash
"/Applications/Balance.app/Contents/Frameworks/vendor/llama/llama-server" --model ~/Library/Application\ Support/Balance/models/Qwen3.5-4B-Q4_K_M.gguf --host 127.0.0.1 --port 5052 --ctx-size 8192 --load-mode none --n-gpu-layers 999
```

(Port 5052 on purpose, so it cannot collide with the app's. Stop it with
Ctrl-C. If the path is not found, look under `Contents/Resources/` instead.)

- **By hand it works, from the app it does not** → the difference is the
  environment the app spawns it in. The app forces `DYLD_LIBRARY_PATH` to the
  binary's own folder and inherits everything else from a PyInstaller-frozen
  process (`model_runtime.ensure_running`). Compare `env` between the two.
- **It fails by hand too** → read what it prints. That is the real error, and
  everything before this was chasing the wrong thing.

## When it works

Tell Julius. He cuts the release on his Mac:

```bash
./scripts/release.sh 1.10.7
```

`VERSION` is already at 1.10.7 and the fix is pushed to `main` — the tag is all
that is missing.

---

## A note on how this went

Six releases went out against guesses made from a distance, and each one cost a
round trip. The thing that finally settled the GPU question was running one
command on this Mac. **Prefer running something here over reasoning about it.**

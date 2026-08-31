#!/bin/bash
# Why is Balance AI not answering on this Mac?
#
#     curl -fsSL https://raw.githubusercontent.com/JuliusPaulin/balance-app/main/scripts/check_ai.sh | bash
#
# Reads and reports. It asks the assistant one question at the end, which is
# read-only — the assistant cannot change anything — and changes nothing else.
#
# Plain shell on purpose: a Mac has curl, grep and awk, and may not have python3.

M="$HOME/Library/Application Support/Balance/models"
GGUF="$M/Qwen3.5-4B-Q4_K_M.gguf"
WANT=2740937888
APP=http://127.0.0.1:5050

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
field() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^,\"}]*\).*/\1/p" | head -1; }

say "This Mac"
echo "  macOS $(sw_vers -productVersion)  $(uname -m)  $(sysctl -n hw.ncpu) cores  $(( $(sysctl -n hw.memsize) / 1073741824 ))GB"

say "Balance"
# A cookie jar, because the CSRF check is double-submit: the token has to come
# back in the header *and* in the cookie it was minted with. Without the jar
# every request here is refused and the script reports a working app as broken.
JAR=$(mktemp)
trap 'rm -f "$JAR"' EXIT
ME=$(curl -s --max-time 8 -c "$JAR" "$APP/api/me")
if [ -z "$ME" ]; then
    echo "  Not running. Open Balance, then run this again."
    exit 0
fi
V=$(printf '%s' "$ME" | field version)
echo "  version $V"
[ "$V" = "dev" ] && echo "  ^ this build has no version in it — it is an old one, reinstall"

say "The model file"
if [ ! -f "$GGUF" ]; then
    echo "  MISSING — Balance AI has not downloaded it yet."
else
    S=$(stat -f%z "$GGUF")
    if [ "$S" = "$WANT" ]; then
        echo "  complete ($S bytes), starts with $(head -c 4 "$GGUF")"
    else
        echo "  INCOMPLETE: $S bytes, short by $((WANT - S))."
        echo "  Delete it and let Balance fetch it again:"
        echo "    rm \"$GGUF\""
    fi
fi
[ -f "$GGUF.part" ] && echo "  a part-file is present — a download stopped early"

say "How the model is running"
ARGS=$(ps -Ao args | grep "[l]lama-server" | head -1)
if [ -z "$ARGS" ]; then
    echo "  The model server is not running."
else
    GPU=$(printf '%s' "$ARGS" | sed -n 's/.*--n-gpu-layers \([0-9]*\).*/\1/p')
    MMAP=$(printf '%s' "$ARGS" | grep -c -- "--load-mode none")
    if [ "$GPU" = "0" ]; then
        echo "  ON THE CPU — this is the slow last resort (about 26 tokens/sec)."
        echo "  Expect two minutes or more per question."
    elif [ "$MMAP" = "1" ]; then
        echo "  On the GPU, without the memory map. This is the intended fix."
    else
        echo "  On the GPU, memory-mapped. The fastest way, and the usual one."
    fi
fi

say "What the model server said"
if [ -f "$M/server.log" ]; then
    grep -icE "GGML_ASSERT|abort" "$M/server.log" | awk '{print "  crashes recorded in the log: "$1" (earlier failed attempts are expected)"}'
    grep -oE "[0-9.]+ tokens per second" "$M/server.log" | tail -2 | awk '{print "  speed: "$0}'
    grep -iE "error|failed|assert" "$M/server.log" | grep -viE "^[[:space:]]*[0-9]+[[:space:]]" | tail -3 | sed 's/^/  /'
else
    echo "  No log — either nothing has started it yet, or this build predates"
    echo "  the log (anything before 1.10.0)."
fi

say "Asking it a question"
TOKEN=$(printf '%s' "$ME" | field csrf_token)
START=$(date +%s)
REPLY=$(curl -s --max-time 420 -X POST "$APP/api/chat" -b "$JAR" -c "$JAR" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $TOKEN" \
    -d '{"messages":[{"role":"user","content":"what did I spend on groceries last month?"}]}')
SECS=$(( $(date +%s) - START ))
# The reply comes back as JSON, so the euro sign and its non-breaking space
# arrive escaped. Put them back rather than printing \u20ac at a person.
ANSWER=$(printf '%s' "$REPLY" | field reply \
         | sed -e 's/\\u20ac/€/g' -e 's/\\u00a0/ /g')
if [ -n "$ANSWER" ]; then
    echo "  ${SECS}s: $ANSWER"
    if [ "$SECS" -gt 60 ]; then
        echo "  It works, but slowly — see 'How the model is running' above."
    else
        echo "  Working normally."
    fi
else
    echo "  ${SECS}s: no answer."
    printf '%s' "$REPLY" | head -c 300 | sed 's/^/  /'
    echo
fi
echo

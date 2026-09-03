#!/usr/bin/env bash
#
# dev_db.sh — set up the local Postgres dev environment for the expense tracker.
#
# Idempotent and re-runnable: ensures the postgresql@16 Homebrew service is
# running, creates the `expense_dev` and `expense_test` databases if they don't
# already exist, and prints the local connection strings.
#
# postgresql@16 is keg-only, so we resolve its bin directory via
# `brew --prefix postgresql@16` rather than relying on it being on PATH.
#
# Schema creation is NOT done here — that comes from data/schema.py once the schema
# is ported (Step 1.2). After this script runs the databases exist but are empty.
#
set -euo pipefail

FORMULA="postgresql@16"
DBS=("expense_dev" "expense_test")

if ! command -v brew >/dev/null 2>&1; then
  echo "error: Homebrew (brew) not found on PATH." >&2
  exit 1
fi

if ! brew list --formula "$FORMULA" >/dev/null 2>&1; then
  echo "error: $FORMULA is not installed. Run: brew install $FORMULA" >&2
  exit 1
fi

PGBIN="$(brew --prefix "$FORMULA")/bin"
CREATEDB="$PGBIN/createdb"
PSQL="$PGBIN/psql"
PG_ISREADY="$PGBIN/pg_isready"

echo "==> Using Postgres tools from: $PGBIN"

# Ensure the service is running (idempotent — `start` is a no-op if already up).
echo "==> Ensuring $FORMULA service is running..."
brew services start "$FORMULA" >/dev/null

# Wait for the server to accept connections.
echo "==> Waiting for Postgres to accept connections..."
for _ in $(seq 1 30); do
  if "$PG_ISREADY" -q; then
    break
  fi
  sleep 1
done
if ! "$PG_ISREADY" -q; then
  echo "error: Postgres did not become ready in time." >&2
  "$PG_ISREADY" || true
  exit 1
fi
"$PG_ISREADY"

# Create databases if missing (idempotent).
for db in "${DBS[@]}"; do
  if "$PSQL" -tAc "SELECT 1 FROM pg_database WHERE datname = '$db';" postgres | grep -q 1; then
    echo "==> Database '$db' already exists (ok)."
  else
    "$CREATEDB" "$db"
    echo "==> Created database '$db'."
  fi
done

echo ""
echo "==> Local connection strings:"
echo "    dev : postgresql://localhost/expense_dev"
echo "    test: postgresql://localhost/expense_test"
echo ""
echo "==> Connect with: \"$PSQL\" postgresql://localhost/expense_dev"
echo "==> Done. (Databases are empty until the schema is created — Step 1.2 / data/schema.py.)"

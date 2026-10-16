#!/usr/bin/env bash
# Weekly run: seed -> scan -> score -> card.  Meant for Monday 05:00 America/Chicago (cron or GitHub Actions).
#
#   * Stops at the first failing step (set -e), so a failed scan never leads to a post.
#   * `bd card` posts to Teams ONLY if TEAMS_WEBHOOK_URL is set; BD_DRY_RUN=1 forces a dry run.
#   * The run date is the America/Chicago date, whatever timezone the host uses (override: BD_AS_OF).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
: "${ANTHROPIC_MODEL:?set ANTHROPIC_MODEL}"

if [ -n "${BD_BIN:-}" ]; then bd=("$BD_BIN")
elif [ -x .venv/bin/bd ]; then bd=(.venv/bin/bd)
else bd=(bd); fi

as_of="${BD_AS_OF:-$(TZ=America/Chicago date +%F)}"

seed_args=(accounts.csv)
if [ -f exclusions.csv ]; then seed_args+=(--exclusions exclusions.csv); fi

card_args=()
if [ "${BD_DRY_RUN:-0}" = "1" ]; then card_args+=(--dry-run); fi

echo "== weekly run for ${as_of} (dry run: ${BD_DRY_RUN:-0})"
"${bd[@]}" seed "${seed_args[@]}"          # idempotent: keeps the database in step with the reviewed CSVs
"${bd[@]}" scan --as-of "${as_of}"
"${bd[@]}" pe-signals --as-of "${as_of}"   # firm-level signals; a no-op when there are no PE firms yet
"${bd[@]}" score --as-of "${as_of}"
"${bd[@]}" card --as-of "${as_of}" ${card_args[@]+"${card_args[@]}"}
echo "== weekly run finished"

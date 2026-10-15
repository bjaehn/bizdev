#!/usr/bin/env bash
# Prints "run" if it is Monday between 05:00 and 05:59 in America/Chicago, otherwise "skip".
# GitHub Actions cron is UTC-only, so the workflow schedules both possible UTC hours (10:00 for CDT,
# 11:00 for CST) and this gate lets exactly one of them through, whatever the season.
# GATE_NOW overrides the clock for testing (any string GNU `date -d` understands).
set -euo pipefail
now="$(TZ=America/Chicago date -d "${GATE_NOW:-now}" +'%u %H')"
if [ "$now" = "1 05" ]; then echo run; else echo skip; fi

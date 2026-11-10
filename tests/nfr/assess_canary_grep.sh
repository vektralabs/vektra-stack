#!/usr/bin/env bash
# REQ-080 canary grep (plan assess-privacy-retention 12b).
#
# After a full discussion seeded with canary strings (in the student's turns, the submitted
# files and the teacher's solution), no canary may appear in any operator-visible sink: the API,
# worker and parser logs, the mail sink's server log and the API's /metrics output.
#
# Usage:
#   assess_canary_grep.sh --canaries CANARIES --seed SEED SINK [SINK...]
#
#   CANARIES  one fixed string per line (blank lines and lines starting with # ignored)
#   SEED      the seeded input the canaries were planted in (proves they were planted)
#   SINK      a captured sink: `docker compose logs vektra > api.log`, the worker and parser logs,
#             the mail sink log, `curl -s .../metrics > metrics.txt`
#
# A grep that finds nothing to grep fails instead of passing. Exit codes:
#   0  every sink was read and no canary was found
#   1  at least one canary was found (the sink and the canary are named, never the line)
#   2  the check could not prove anything: no canary, a canary absent from the seed, or a
#      sink that is missing or empty
set -euo pipefail

usage() {
    echo "usage: $0 --canaries FILE --seed FILE SINK [SINK...]" >&2
    exit 2
}

canaries="" seed=""
while [ $# -gt 0 ]; do
    case "$1" in
        --canaries) [ $# -ge 2 ] || usage; canaries="$2"; shift 2 ;;
        --seed) [ $# -ge 2 ] || usage; seed="$2"; shift 2 ;;
        --help|-h) usage ;;
        --*) echo "unknown option: $1" >&2; usage ;;
        *) break ;;
    esac
done
[ -n "$canaries" ] && [ -n "$seed" ] && [ $# -gt 0 ] || usage

for f in "$canaries" "$seed"; do
    if [ ! -s "$f" ]; then
        echo "BLOCKED: $f is missing or empty" >&2
        exit 2
    fi
done

# bash 3.2 (macOS) has no mapfile
list=()
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ''|'#'*) ;;
        *) list+=("$line") ;;
    esac
done < "$canaries"
if [ "${#list[@]}" -eq 0 ]; then
    echo "BLOCKED: no canary in $canaries" >&2
    exit 2
fi

# false-pass guard: every canary must really be in the seeded input
for c in "${list[@]}"; do
    if ! grep -qF -- "$c" "$seed"; then
        echo "BLOCKED: a canary is not in the seeded input $seed (line $(grep -nxF -- "$c" "$canaries" | cut -d: -f1))" >&2
        exit 2
    fi
done

for sink in "$@"; do
    if [ ! -s "$sink" ]; then
        echo "BLOCKED: sink $sink is missing or empty" >&2
        exit 2
    fi
done

found=0
for sink in "$@"; do
    i=0
    for c in "${list[@]}"; do
        i=$((i + 1))
        if grep -qF -- "$c" "$sink"; then
            echo "LEAK: canary #$i found in $sink" >&2
            found=1
        fi
    done
done

if [ "$found" -ne 0 ]; then
    exit 1
fi
echo "ok: ${#list[@]} canaries, $# sinks, 0 hits"

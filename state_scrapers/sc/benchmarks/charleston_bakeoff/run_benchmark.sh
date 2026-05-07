#!/usr/bin/env bash
# Charleston County SC HOA discovery bake-off
#   Each (model, effort) cell gets WALLCLOCK seconds of agent runtime.
#   $5 cost cap is a safety net; wall-clock is intended to be binding.
#   We want to know: per "session" of Max subscription work, which combo banks the most HOAs?
#
# Order: model outer (opus -> sonnet -> haiku), effort inner (low -> medium -> high).
# Front-loads Opus so partial results are most informative if interrupted.
set -euo pipefail

WALLCLOCK="${WALLCLOCK_SECONDS:-900}"   # 15 min per cell by default
COST_CAP="${COST_CAP_USD:-5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TASK_FILE="$SCRIPT_DIR/task.txt"
RESULTS_DIR="$SCRIPT_DIR/results/$(date +%Y%m%d_%H%M%S)"
SUMMARY_FILE="$RESULTS_DIR/summary.tsv"

mkdir -p "$RESULTS_DIR"
echo -e "run\tmodel\teffort\tcost_usd\tinput_tokens\toutput_tokens\tcache_read\tcache_create\thoas_banked\tpdfs_banked\tstop_reason\tduration_ms\tterminated_by" > "$SUMMARY_FILE"

MODELS=(
  "claude-opus-4-7	opus"
  "claude-sonnet-4-6	sonnet"
  "claude-haiku-4-5-20251001	haiku"
)
EFFORTS=("low" "medium" "high")

parse_result() {
  local jsonl="$1"
  python3 - "$jsonl" <<'PYEOF'
import json, sys

f = sys.argv[1]
result = None
for line in open(f, errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "result":
        result = d
        break

if result is None:
    print("\t".join(["NA"] * 7))
    sys.exit(0)

cost = result.get("total_cost_usd", "NA")
subtype = result.get("subtype", "NA")
duration = result.get("duration_ms", "NA")
usage = result.get("usage", {}) or {}
itok = usage.get("input_tokens", "NA")
otok = usage.get("output_tokens", "NA")
cread = usage.get("cache_read_input_tokens", "NA")
ccreate = usage.get("cache_creation_input_tokens", "NA")

print("\t".join(str(x) for x in [cost, itok, otok, cread, ccreate, subtype, duration]))
PYEOF
}

gcs_count_manifests() {
  gcloud storage ls "gs://hoaproxy-bank/v1/SC/**/manifest.json" 2>/dev/null | wc -l | tr -d ' '
}
gcs_count_pdfs() {
  gcloud storage ls -r "gs://hoaproxy-bank/v1/SC/**" 2>/dev/null | grep "original\.pdf" | wc -l | tr -d ' '
}

# Run a command with a hard wall-clock timeout (seconds). Returns 124 on timeout.
gcs_with_timeout() {
  local seconds=$1
  shift
  "$@" &
  local pid=$!
  ( sleep "$seconds"
    if kill -0 "$pid" 2>/dev/null; then
      pkill -TERM -P "$pid" 2>/dev/null || true
      sleep 2
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
    fi
  ) &
  local watcher=$!
  set +e
  wait "$pid"
  local rc=$?
  set -e
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return $rc
}

# Recursively kill a pid and ALL its descendants (grandchildren too).
# pkill -P only handles direct children, which leaks orphans like
# python -m hoaware.discovery probe-batch when claude spawned them via bash.
kill_tree() {
  local sig=$1
  local pid=$2
  local children
  children=$(pgrep -P "$pid" 2>/dev/null || true)
  for c in $children; do
    kill_tree "$sig" "$c"
  done
  kill "-$sig" "$pid" 2>/dev/null || true
}
export -f kill_tree

run_with_walltime() {
  # $1 = wall-clock seconds, rest = command. Kills the command + its full descendant tree.
  local seconds=$1
  shift
  "$@" &
  local pid=$!
  ( sleep "$seconds"
    if kill -0 "$pid" 2>/dev/null; then
      kill_tree TERM "$pid"
      sleep 3
      kill_tree KILL "$pid"
      echo "WALLCLOCK_HIT" > /tmp/benchmark_terminator.flag
    fi
  ) &
  local watcher=$!
  set +e
  wait "$pid"
  local rc=$?
  set -e
  # Defensive: even if claude exited cleanly, sweep any descendants it left behind
  # (e.g., probe-batch processes that escaped the agent's own cleanup).
  kill_tree KILL "$pid" 2>/dev/null || true
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return $rc
}

RUN_NUM=0
for model_line in "${MODELS[@]}"; do
  MODEL_ID=$(echo "$model_line" | cut -f1)
  MODEL_NAME=$(echo "$model_line" | cut -f2)

  for EFFORT in "${EFFORTS[@]}"; do
    RUN_NUM=$((RUN_NUM + 1))
    RUN_NAME="${MODEL_NAME}_${EFFORT}"
    OUTFILE="$RESULTS_DIR/${RUN_NAME}.jsonl"

    echo ""
    echo "============================================="
    echo "  Run $RUN_NUM/9: $RUN_NAME  (wall=${WALLCLOCK}s  cost_cap=\$${COST_CAP})"
    echo "  Started: $(date)"
    echo "============================================="

    # Clean SC bank prefix so we count only this cell's output
    gcs_with_timeout 180 gcloud storage rm -r "gs://hoaproxy-bank/v1/SC/" 2>/dev/null || true

    rm -f /tmp/benchmark_terminator.flag

    cd "$PROJECT_DIR"
    set +e
    run_with_walltime "$WALLCLOCK" \
      claude -p "$(cat "$TASK_FILE")" \
        --model "$MODEL_ID" \
        --effort "$EFFORT" \
        --max-budget-usd "$COST_CAP" \
        --output-format stream-json --verbose \
        --dangerously-skip-permissions \
        > "$OUTFILE" 2>&1
    set -e

    if [ -f /tmp/benchmark_terminator.flag ]; then
      TERMINATED_BY="wallclock"
    else
      TERMINATED_BY="natural"
    fi

    PARSED=$(parse_result "$OUTFILE")
    COST=$(echo "$PARSED" | cut -f1)
    ITOK=$(echo "$PARSED" | cut -f2)
    OTOK=$(echo "$PARSED" | cut -f3)
    CREAD=$(echo "$PARSED" | cut -f4)
    CCREATE=$(echo "$PARSED" | cut -f5)
    STOP=$(echo "$PARSED" | cut -f6)
    DURATION=$(echo "$PARSED" | cut -f7)

    HOAS=$(gcs_count_manifests)
    PDFS=$(gcs_count_pdfs)

    echo -e "${RUN_NUM}\t${MODEL_NAME}\t${EFFORT}\t${COST}\t${ITOK}\t${OTOK}\t${CREAD}\t${CCREATE}\t${HOAS}\t${PDFS}\t${STOP}\t${DURATION}\t${TERMINATED_BY}" >> "$SUMMARY_FILE"
    echo "  -> cost=\$${COST}  in=${ITOK}  out=${OTOK}  HOAs=${HOAS}  PDFs=${PDFS}  stop=${STOP}  killed_by=${TERMINATED_BY}"

    # Archive this cell's bank output before the next cell wipes the prefix.
    # Use cp+rm rather than mv because gcloud storage mv can wedge on parallel ops;
    # 5-min hard cap so a stuck archive doesn't block the rest of the benchmark.
    ARCHIVE_PREFIX="gs://hoaproxy-bank/benchmark/$(basename "$RESULTS_DIR")/${RUN_NAME}"
    if gcs_with_timeout 300 gcloud storage cp -r "gs://hoaproxy-bank/v1/SC" "$ARCHIVE_PREFIX/" 2>/dev/null; then
      echo "  archived -> $ARCHIVE_PREFIX/SC"
    else
      echo "  WARNING: archive timed out / failed for $RUN_NAME (data still live in v1/SC; will be wiped next cell)"
    fi
  done
done

echo ""
echo "============================================="
echo "  BENCHMARK COMPLETE"
echo "============================================="
echo ""
column -t -s $'\t' "$SUMMARY_FILE"
echo ""
echo "Full results in: $RESULTS_DIR"

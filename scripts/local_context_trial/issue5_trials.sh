#!/usr/bin/env bash
set -eu

if [[ ${1:-} == --plan ]]; then
    mode=--plan
    shift
else
    mode=--checkpoint
fi

profile=${1:-}
if ! { [[ $profile == fast && $# == 3 ]] || [[ $profile == long && $# == 4 ]]; }; then
    echo 'Usage: bash issue5_trials.sh [--plan] fast MODEL RESULTS_DIRECTORY' >&2
    echo '       bash issue5_trials.sh [--plan] long MODEL RESULTS_DIRECTORY FROZEN_MANIFEST' >&2
    exit 2
fi

model=$2
results=$3
trial_dir=$(cd "$(dirname "$0")" && pwd)
python_bin=${TRIAL_PYTHON:-"$trial_dir/../../.venv/bin/python"}
common=("$mode" --model "$model" --rounds 1 --specs vague --map-tokens 1024 --seed 20261123
        --no-auto-lint)
if [[ -n ${TRIAL_THINKING_TOKENS:-} ]]; then
    common+=(--thinking-tokens "$TRIAL_THINKING_TOKENS")
fi

if [[ $profile == fast ]]; then
    "$python_bin" "$trial_dir/heldout.py" "${common[@]}" --suite issue5 --arms passive,audit \
        --local-callers-backend builtin --no-local-callers-read-range --no-facts \
        --out "$results/fast.jsonl"
    exit
fi

manifest=$4
"$python_bin" - "$manifest" <<'PY'
import json
from pathlib import Path
import sys

if len(json.loads(Path(sys.argv[1]).read_text())["cases"]) != 30:
    sys.exit("The 240-attempt profile requires a frozen 30-case replay manifest.")
PY

replay=("${common[@]}" --suite replay --replay-manifest "$manifest")
"$python_bin" "$trial_dir/heldout.py" "${replay[@]}" \
    --arms baseline,passive,contract,audit,contract+audit \
    --local-callers-backend builtin --no-local-callers-read-range --no-facts \
    --out "$results/stage1.jsonl"
"$python_bin" "$trial_dir/heldout.py" "${replay[@]}" --arms passive \
    --local-callers-backend cymbal --no-local-callers-read-range --no-facts \
    --out "$results/stage2-cymbal.jsonl"
"$python_bin" "$trial_dir/heldout.py" "${replay[@]}" --arms passive \
    --local-callers-backend builtin --local-callers-read-range --no-facts \
    --out "$results/stage2-readrange.jsonl"
"$python_bin" "$trial_dir/heldout.py" "${replay[@]}" --arms passive \
    --local-callers-backend builtin --no-local-callers-read-range --facts \
    --out "$results/stage2-facts.jsonl"

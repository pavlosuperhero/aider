#!/usr/bin/env bash
set -eu

if [ "$#" -ne 2 ]; then
    echo "Usage: bash issue5_input_trial.sh MODEL NEW_RESULTS_DIRECTORY" >&2
    exit 2
fi

model=$1
results=$2
trial_dir=$(cd "$(dirname "$0")" && pwd)
python_bin=${TRIAL_PYTHON:-"$trial_dir/../../.venv/bin/python"}

for suite in issue5 requests; do
    "$python_bin" "$trial_dir/heldout.py" --suite "$suite" --check-fixtures
done

# Each JSONL is append-only. A fresh directory keeps different runs out of the same pairs.
mkdir "$results"
common=(--model "$model" --rounds 3 --specs vague --map-tokens 1024 --seed 20261123
        --arms passive --local-callers-backend builtin)
"$python_bin" "$trial_dir/heldout.py" "${common[@]}" --preset issue5-economy \
    --out "$results/issue5.jsonl"
"$python_bin" "$trial_dir/heldout.py" "${common[@]}" --suite requests \
    --read-range-modes off,on --no-facts --out "$results/requests.jsonl"

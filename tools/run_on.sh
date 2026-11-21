#!/bin/sh
# Run an already built image on a data dir (tests.json, commits/, answers.json)
# under the scorer's limits, then score and show the per-commit breakdown.
# Usage: sh tools/run_on.sh <data dir> [image] [output file]
set -e
DATA_ARG=${1:?data dir}
IMAGE=${2:-blast-radius-entry}
OUT=${3:-output.jsonl}
DATA=$(cd "$DATA_ARG" && (pwd -W 2>/dev/null || pwd))
export MSYS_NO_PATHCONV=1
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$DATA/commits:/data/commits:ro" \
  --volume "$DATA/tests.json:/data/tests.json:ro" \
  "$IMAGE" > "$OUT" 2> "$OUT.log" || { tail -20 "$OUT.log"; exit 1; }
tail -1 "$OUT.log"
python score.py "$DATA_ARG" "$DATA_ARG/answers.json" "$OUT" | head -3
python tools/breakdown.py "$DATA_ARG" "$DATA_ARG/answers.json" "$OUT"

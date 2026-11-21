#!/bin/sh
# What `make sample` does, for machines without make: build, run once on the sample
# commits under the scorer's limits, and score. Usage: sh run_sample.sh [image]
set -e
IMAGE=${1:-blast-radius-entry}
HERE=$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)
export MSYS_NO_PATHCONV=1
docker build --platform linux/amd64 --tag "$IMAGE" . >/dev/null
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$HERE/sample/commits:/data/commits:ro" \
  --volume "$HERE/sample/tests.json:/data/tests.json:ro" \
  "$IMAGE" > output.jsonl
python score.py sample sample/answers.json output.jsonl

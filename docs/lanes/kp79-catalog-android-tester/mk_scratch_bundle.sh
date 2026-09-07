#!/usr/bin/env bash
# usage: mk_scratch.sh <repo-path> <out-yaml>
REPO="$1"; OUT="$2"
CACHE=/home/bkrabach/.amplifier/cache
cat > "$OUT" <<EOF
bundle:
  name: kp79-scratch
  version: 0.1.0
  description: Scratch session used only to render the delegate agent catalog.

session:
  orchestrator:
    module: loop-streaming
    source: file://${CACHE}/amplifier-module-loop-streaming-b0b975ea6a1072dd
  context:
    module: context-simple
    source: file://${CACHE}/amplifier-module-context-simple-381762bb39b0b3fa

include:
  - file://${REPO}/behaviors/android-tester.yaml

tools:
  - module: tool-delegate
    source: file://${CACHE}/amplifier-foundation-c909465861f9d6ce/modules/tool-delegate
EOF

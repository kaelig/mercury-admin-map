#!/usr/bin/env bash
# Run one recorder batch as a lean headless Claude Code process (SKILL.md, Recorders).
#
#   tools/a11y/record-headless.sh <run> <batch>       e.g. 2026-10-02-1349 record-003
#
# Sonnet with only the Bash, Read and Write tools: no MCP servers, skills, settings files, CLAUDE.md
# or auto-memory, and a one-paragraph system prompt. The first turn's context is about 3k tokens,
# against 44k for an Agent-tool subagent, and every later turn re-reads it. Permissions are dontAsk:
# a tool call the allowlist does not match is refused, never prompted. The agent-browser session is
# named after the batch.
#
# The recorder writes a11y/runs/<run>/batches/<batch>.out.jsonl with the Write tool, once, after the
# last state; the Edit rule on that one path allows it, and nothing else in the repo is writable.
# A crash mid-batch loses the out file, but the recipes it checked are already written.
# Deny rules back the prompt's token rules: no tools/a11y source, no recipe files, no listing
# shots/, no git, no screenshot path with "..".
#
# The stream goes to a11y/runs/<run>/batches/<batch>.stream.jsonl (gitignored), not to stdout, so the
# caller's context stays small. Token usage and cost print to stderr at the end.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <run> <batch>" >&2
  exit 2
fi
RUN=$1
BATCH=$2
REPO=$(cd "$(dirname "$0")/../.." && pwd)
BATCH_FILE=a11y/runs/$RUN/batches/$BATCH.json
OUT=a11y/runs/$RUN/batches/$BATCH.out.jsonl
STREAM=$REPO/a11y/runs/$RUN/batches/$BATCH.stream.jsonl
MESSAGE="Read .agents/skills/a11y-audit/references/recorder.md and follow it. Batch: $BATCH_FILE. agent-browser session: $BATCH."

if [ ! -f "$REPO/$BATCH_FILE" ]; then
  echo "no batch file $REPO/$BATCH_FILE" >&2
  exit 2
fi

SYSTEM="You are a recorder subagent working in the repository at $REPO, which is your working \
directory. The user message names a prompt file: read it and follow it exactly. It defines your \
task, the only commands you may run and the output you write. Write each command out in full, \
without shell variables, cd or eval: a command the prompt does not name is refused. Be terse: no \
plans, recaps or commentary between tool calls."

AB="agent-browser --session $BATCH"
ALLOW=(
  "Read(./**)"
  "Bash(python3 tools/a11y/audit.py --help)"
  "Bash(python3 tools/a11y/audit.py reach *)"
  "Bash(node tools/a11y/recipe.mjs check *)"
  "Bash(agent-browser --help)"
  "Bash($AB snapshot *)"
  "Bash($AB click *)"
  "Bash($AB hover *)"
  "Bash($AB check *)"
  "Bash($AB fill *)"
  "Bash($AB select *)"
  "Bash($AB press *)"
  "Bash($AB scrollintoview *)"
  "Bash($AB wait *)"
  "Bash($AB get url)"
  "Bash($AB screenshot a11y/runs/$RUN/shots/*)"
  "Bash(jq *)"
  "Bash(head *)"
  "Bash(tail *)"
  "Bash(grep *)"
  "Bash(wc *)"
  "Edit($OUT)"
)
DENY=(
  "Read(tools/**)"
  "Read(a11y/recipes/**)"
  "Bash(cat *)"
  "Bash(sed *)"
  "Bash(ls *)"
  "Bash(find *)"
  "Bash(grep * tools/*)"
  "Bash(grep * a11y/recipes*)"
  "Bash(git *)"
  "Bash($AB screenshot *..*)"
)

cd "$REPO"
set +e
printf '%s' "$MESSAGE" | CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 claude -p \
  --model sonnet \
  --tools Bash,Read,Write \
  --strict-mcp-config \
  --disable-slash-commands \
  --setting-sources '' \
  --permission-mode dontAsk \
  --system-prompt "$SYSTEM" \
  --allowedTools "${ALLOW[@]}" \
  --disallowedTools "${DENY[@]}" \
  --output-format stream-json \
  --verbose \
  > "$STREAM"
status=$?
set -e

python3 "$REPO/tools/a11y/stream_usage.py" "$STREAM" >&2
exit "$status"

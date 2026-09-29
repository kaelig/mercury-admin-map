#!/bin/bash
# DOM sweep for clickable text ending in punctuation, across every captured route, in 8 parallel
# headless agent-browser sessions. Writes out/res<k>.jsonl; then run `python3 judge.py` (needs
# JEV_API_KEY in the project .env) to score each candidate with Jev.
# Books routes need the demo's "Try Mercury Books" setup first and lose it on reload, so they
# redirect to /dashboard here; see README.md for how they were covered.
cd "$(dirname "$0")"; mkdir -p out; N=8
python3 - "$N" <<'PY'
import json, sys
m = json.load(open("../../build/map.json")); n = int(sys.argv[1])
P = sorted({x["url"] for a in m["areas"] for x in a["nodes"]})
P = [u for u in P if not any(s in u for s in ("…", "<", "...", "{"))]
for k in range(n): open(f"out/chunk{k}.txt", "w").write("\n".join(P[k::n]) + "\n")
PY
run() {
  k=$1; s=lp$k; out=out/res$k.jsonl; : > $out
  agent-browser --session $s set viewport 1440 900 >/dev/null 2>&1
  while read -r p; do
    [ -z "$p" ] && continue
    agent-browser --session $s open "https://demo.mercury.com$p" >/dev/null 2>&1
    agent-browser --session $s wait --load networkidle >/dev/null 2>&1
    agent-browser --session $s wait 1500 >/dev/null 2>&1
    r=$(agent-browser --session $s eval --stdin < scan.js 2>/dev/null)
    python3 -c 'import json,sys
try: d=json.loads(json.loads(sys.argv[2]))
except Exception as e: d={"err":str(e)[:200]}
d["path"]=sys.argv[1]; print(json.dumps(d))' "$p" "$r" >> $out
  done < out/chunk$k.txt
  agent-browser --session $s close >/dev/null 2>&1
}
for k in $(seq 0 $((N-1))); do run $k & done; wait
cat out/res*.jsonl | wc -l

#!/usr/bin/env python3
"""Sum token usage from a Claude Code stream-json file (or a session transcript .jsonl).

    python3 tools/a11y/stream_usage.py <file.jsonl> [--json]

One assistant message can span several lines (one per content block), each repeating a usage
snapshot, so lines are deduplicated by message id. Input, cache write and cache read are the same
on every line of a message, so their per-message sums are exact. Output is not: each line carries
the output count from when that line was emitted, often only the first few tokens. So output comes
from the result line's usage, which is cumulative, when the file has one. Without a result line
(a session transcript), output is the largest count seen per message, which may still be partial.

Prints turns (assistant messages), input, cache write, cache read, output, processed total (the
sum of the four), the first turn's context size, and cost when a result line carries
total_cost_usd.
"""
import json
import sys

INPUTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def summarize(path):
    inputs, output_max, order = {}, {}, []
    cost, model, result = None, None, None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            kind = line.get("type")
            if kind == "system" and line.get("subtype") == "init":
                model = line.get("model")
            elif kind == "result":
                result = line
                if line.get("total_cost_usd") is not None:
                    cost = line["total_cost_usd"]
            elif kind == "assistant":
                msg = line.get("message") or {}
                mid, u = msg.get("id"), msg.get("usage")
                if not mid or not u:
                    continue
                if mid not in inputs:
                    order.append(mid)
                inputs[mid] = {k: u.get(k) or 0 for k in INPUTS}
                output_max[mid] = max(output_max.get(mid, 0), u.get("output_tokens") or 0)
                model = model or msg.get("model")
    totals = {k: sum(inputs[m][k] for m in order) for k in INPUTS}
    final = (result or {}).get("usage") or {}
    if final.get("output_tokens") is not None:
        output, source = final["output_tokens"], "result"
        mismatch = {k: final.get(k) for k in INPUTS if final.get(k) is not None and final[k] != totals[k]}
    else:
        output, source, mismatch = sum(output_max.values()), "per-message max, may be partial", {}
    first = inputs[order[0]] if order else None
    out = {
        "file": path,
        "model": model,
        "turns": len(order),
        "input": totals["input_tokens"],
        "cacheWrite": totals["cache_creation_input_tokens"],
        "cacheRead": totals["cache_read_input_tokens"],
        "output": output,
        "outputSource": source,
        "processed": sum(totals.values()) + output,
        "firstTurnContext": sum(first.values()) if first else None,
        "costUsd": cost,
    }
    if mismatch:
        out["resultUsageMismatch"] = mismatch
    if result:
        out["numTurns"] = result.get("num_turns")
        out["durationMs"] = result.get("duration_ms")
        out["isError"] = result.get("is_error")
    return out


def main(argv):
    args = [a for a in argv if a != "--json"]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    s = summarize(args[0])
    if "--json" in argv:
        print(json.dumps(s))
        return 0
    print(f"model {s['model']}  turns {s['turns']}")
    print(f"input {s['input']:,}  cache write {s['cacheWrite']:,}  cache read {s['cacheRead']:,}  output {s['output']:,}")
    print(f"processed {s['processed']:,}  first-turn context {s['firstTurnContext'] or 0:,}")
    if s["outputSource"] != "result":
        print("note: no result line; output is the largest count seen per message and may be partial")
    if "resultUsageMismatch" in s:
        print(f"note: result usage differs from the per-message sums: {s['resultUsageMismatch']}")
    if s["costUsd"] is not None:
        print(f"cost ${s['costUsd']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""
Format Claude Code stream-json output into readable CI log lines.

Reads stream-json from stdin, writes human-readable lines to stdout.
Also appends a summary block to $GITHUB_STEP_SUMMARY when running in CI.

Usage:
    python scripts/claude_run.py ... | tee /tmp/raw.jsonl | python .github/stream-formatter.py
"""
import json
import os
import sys
from datetime import datetime

summary_lines: list[str] = []
total_cost_usd = 0.0
num_turns = 0
errors: list[str] = []


def out(line: str) -> None:
    print(line, flush=True)
    summary_lines.append(line)


def _tool_line(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = inp.get("command", "").replace("\n", " ")[:120]
        return f"  > Bash: {cmd}"
    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", inp.get("path", ""))
        return f"  > {name}: {path}"
    if name in ("Glob", "Grep"):
        pat = inp.get("pattern", inp.get("path", ""))
        return f"  > {name}: {pat}"
    if name == "Agent":
        desc = inp.get("description", "")
        subtype = inp.get("subagent_type", "")
        tag = f" [{subtype}]" if subtype else ""
        return f"  > Agent{tag}: {desc}"
    if "figma" in name.lower():
        # e.g. mcp__plugin_figma_figma__get_design_context
        short = name.split("__")[-1]
        node = inp.get("nodeId", inp.get("fileKey", inp.get("fileKey", "")))
        return f"  > figma.{short}: {node}"
    if "slack" in name.lower():
        short = name.split("__")[-1]
        ch = inp.get("channel_id", inp.get("channel", ""))
        return f"  > slack.{short}: {ch}"
    # Generic fallback
    preview = str(inp)[:80] if inp else ""
    return f"  > {name}: {preview}"


for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    try:
        msg = json.loads(raw_line)
    except json.JSONDecodeError:
        out(raw_line)
        continue

    t = msg.get("type", "")

    if t == "system":
        tools = msg.get("tools", [])
        mcps = [
            s["name"].replace("plugin:", "").replace(":slack", "").replace(":figma", "")
            for s in msg.get("mcp_servers", [])
            if s.get("status") == "connected"
        ]
        model = msg.get("model", "?")
        mode = msg.get("permissionMode", "?")
        out(f"[init] model={model} tools={len(tools)} mcp=[{','.join(mcps)}] mode={mode}")

    elif t == "assistant" and "message" in msg:
        for block in msg["message"].get("content", []):
            btype = block.get("type")
            if btype == "text":
                text = block["text"].strip()
                if text:
                    out(text)
            elif btype == "tool_use":
                out(_tool_line(block.get("name", ""), block.get("input", {})))

    elif t == "user":
        # Tool results — log failures for visibility but don't treat as fatal
        # (Claude may handle tool errors gracefully; postcondition check is the real gate)
        for block in msg.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("is_error"):
                    err = str(block.get("content", ""))[:200]
                    out(f"  ! tool_error: {err}")

    elif t == "result":
        total_cost_usd += msg.get("total_cost_usd", 0)
        num_turns = msg.get("num_turns", num_turns)
        dur_s = msg.get("duration_ms", 0) / 1000
        is_error = msg.get("is_error", False)
        stop = msg.get("stop_reason", "?")
        status = "ERROR" if is_error else "ok"
        out(
            f"\n[result] status={status} turns={num_turns} "
            f"time={dur_s:.1f}s cost=${total_cost_usd:.4f} stop={stop}"
        )
        if is_error:
            err_text = msg.get("result", "")[:300]
            out(f"[error] {err_text}")
            errors.append(err_text)

    elif t == "rate_limit_event":
        info = msg.get("rate_limit_info", {})
        rl_status = info.get("status", "?")
        rl_type = info.get("rateLimitType", "?")
        if rl_status != "allowed":
            out(f"[rate-limit] status={rl_status} type={rl_type}")

    elif t == "error":
        err_text = msg.get("error", str(msg))[:300]
        out(f"[ERROR] {err_text}")
        errors.append(err_text)


# GitHub Step Summary
step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
if step_summary:
    with open(step_summary, "a") as f:
        f.write("## claude-run result\n\n")

        # Stats row
        f.write(f"| turns | cost | errors |\n")
        f.write(f"|-------|------|--------|\n")
        f.write(f"| {num_turns} | ${total_cost_usd:.4f} | {len(errors)} |\n\n")

        if errors:
            f.write("### Errors\n\n")
            for e in errors:
                f.write(f"- `{e}`\n")
            f.write("\n")

        # Condensed log (skip init line, cap at 80 lines)
        log_lines = [l for l in summary_lines if not l.startswith("[init]")][:80]
        if log_lines:
            f.write("### Output\n\n```\n")
            f.write("\n".join(log_lines))
            f.write("\n```\n")

sys.exit(1 if errors else 0)

"""Score/chat via the local Claude Code CLI in headless mode — NO API key needed.

This shells out to `claude -p` (Claude Code headless), which reasons using your
existing Claude Code login (subscription), reads the piped stdin as context, and
prints the answer. The same CLAUDE.md rubric is sent so the output matches the
in-session agent.

IMPORTANT: `claude` refuses to run *inside* another Claude Code session (it would
crash both). So this works when the dashboard is launched normally (e.g. the
desktop icon / `dashboard.bat`), NOT from a terminal that is itself a Claude Code
session. We detect that case and raise a clear message.
"""
from __future__ import annotations

import shutil
import subprocess

from scanner.scoring.llm_scorer import CHAT_SYSTEM, RUBRIC


def is_available() -> bool:
    """True if the Claude Code CLI is on PATH."""
    return shutil.which("claude") is not None


def _invoke(instruction: str, body: str, timeout: int = 600) -> str:
    """Run `claude -p <instruction>` with `body` piped to stdin; return stdout."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI ('claude') not found on PATH.")
    low = exe.lower()
    if low.endswith(".ps1"):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, "-p", instruction]
    elif low.endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", exe, "-p", instruction]
    else:
        cmd = [exe, "-p", instruction]
    proc = subprocess.run(cmd, input=body, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or out or "claude -p returned nothing").strip()
        if "inside another Claude Code session" in err:
            raise RuntimeError(
                "Can't use Claude Code from inside a Claude Code session. Launch the "
                "dashboard normally (the desktop icon / dashboard.bat), not from a "
                "Claude Code terminal.")
        raise RuntimeError(err[:800])
    return out


def score(context_pack_md: str) -> str:
    """Rank the context pack into asymmetric signals (Markdown) via Claude Code."""
    body = RUBRIC + "\n\n===== CONTEXT PACK =====\n\n" + context_pack_md
    instr = ("Apply the rubric at the top of the piped input to the context pack that "
             "follows it. Output ONLY the ranked asymmetric-signal markdown (no preamble, "
             "no tool use).")
    return _invoke(instr, body)


def chat(question: str, context: str) -> str:
    """Answer a follow-up grounded in retrieved stored data, via Claude Code."""
    body = (CHAT_SYSTEM + "\n\n===== STORED DATA =====\n\n" + context
            + f"\n\n===== QUESTION =====\n{question}")
    instr = ("Answer the question at the end using ONLY the stored data above it; cite the "
             "source links; research, not investment advice. Output only the answer.")
    return _invoke(instr, body)

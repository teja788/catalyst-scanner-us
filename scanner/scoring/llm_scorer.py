"""LLM scorer for the dashboard's AI rank / chat panels (only used when the user
picks an API engine there). The SDKs are LAZY-IMPORTED inside each function and API
keys are read from the argument or the environment ONLY at call time — importing
this module never needs a key.

The rubric is READ FROM CLAUDE.md at import, so API ranking follows exactly the
rules the in-session agent follows (a hand-copied rubric had drifted and lost the
historical-amount trap, 13D/A direction, 10b5-1 weighting, price check, watchlist).
"""
from __future__ import annotations

import os

from scanner.config import ROOT

DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_OPENAI_MODEL = "gpt-5.5"


def _load_rubric() -> str:
    """The '## Reasoning rubric' section of CLAUDE.md, up to (not incl.) the
    save-to-research-log step, which is a tool action for the in-session agent."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = text.index("## Reasoning rubric")
    end = text.index("### ALWAYS save the analysis", start)
    return ("You surface ASYMMETRIC opportunities among top US-listed companies for "
            "further investigation — idea-generation, NOT investment advice. The context "
            "pack is provided in the user message (instead of runtime/context_pack.md). "
            "Apply this rubric exactly:\n\n" + text[start:end].strip() + "\n")


RUBRIC = _load_rubric()

CHAT_SYSTEM = """You are a research assistant for a US-equities catalyst scanner. Answer the
user's question using ONLY the provided stored data (filings, ownership, news with source
links). Cite the source link for specifics. If the data lacks the answer, say so and suggest
a fresh pull. Research, not investment advice — never recommend buying or selling.
"""


def score(context_pack: str, provider: str = "claude",
          model: str | None = None, api_key: str | None = None) -> str:
    """Rank the context pack into asymmetric signals (Markdown)."""
    user = f"Here is today's context pack. Produce the ranked asymmetric-signal list per the rubric.\n\n{context_pack}"
    if provider == "openai":
        return _openai_complete(RUBRIC, user, model or DEFAULT_OPENAI_MODEL, api_key)
    return _claude_complete(RUBRIC, user, model or DEFAULT_CLAUDE_MODEL, api_key)


def chat(question: str, context: str, provider: str = "claude",
         model: str | None = None, api_key: str | None = None) -> str:
    """Answer a follow-up grounded in retrieved stored data."""
    user = f"Stored data:\n\n{context}\n\nQuestion: {question}"
    if provider == "openai":
        return _openai_complete(CHAT_SYSTEM, user, model or DEFAULT_OPENAI_MODEL, api_key)
    return _claude_complete(CHAT_SYSTEM, user, model or DEFAULT_CLAUDE_MODEL, api_key)


def _claude_complete(system: str, user: str, model: str, api_key: str | None) -> str:
    import anthropic  # lazy — only needed when llm_api mode is used
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=16000,   # room for adaptive thinking (on by default on Opus 5)
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise RuntimeError(f"Claude declined this request (refusal, category={cat}).")
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _openai_complete(system: str, user: str, model: str, api_key: str | None) -> str:
    from openai import OpenAI  # lazy
    client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
    if hasattr(client, "responses"):
        resp = client.responses.create(model=model, instructions=system, input=user)
        if getattr(resp, "output_text", None) is not None:
            return resp.output_text.strip()
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (resp.choices[0].message.content or "").strip()

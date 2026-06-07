"""LLM scorer (Section 17 hook) — OFF by default.

Nothing here is imported unless settings.scoring.mode == "llm_api". The SDKs are
LAZY-IMPORTED inside each function and API keys are read from the argument or the
environment ONLY at call time — importing this module never needs a key.

The same rubric as CLAUDE.md is used as the system prompt so API ranking matches
the in-session agent's behaviour.
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-5.5"

RUBRIC = """You surface ASYMMETRIC opportunities among the top US-listed companies for
further investigation. This is idea-generation, NOT investment advice, never a buy/sell
recommendation.

You are given a CONTEXT PACK assembled by deterministic code: SEC filings (highest trust,
with 8-K item codes), disclosed ownership (13D/13G/Form-4: activist stakes, insider buys,
superinvestor matches), and wires/news (lower trust). Produce a RANKED list.

Judge each: (1) catalyst type & strength; (2) materiality relative to market cap;
(3) novelty/under-the-radar — prefer strong-catalyst + LOW-coverage names; (4) source
credibility (SEC filing > wire/PR > news); (5) plausible forward impact (the mechanism).

THE BAR — be a tough filter; flag FEW high-quality leads. Flag a lead ONLY if it clears
every gate: real forward catalyst with a stated mechanism (not a compliance/process event);
material to size; under-appreciated; substantiated by a hard filing or strong corroboration;
genuinely asymmetric upside. Otherwise move it to "Watch" or omit. Prefer FEW leads; most
days "Nothing notable today." is correct. Keep SEC FILINGS separate from NEWS; always keep
source links; never imply certainty; never give buy/sell advice.

Output format per lead: a numbered "**TICKER — Company**" with bullets for What happened /
Why asymmetric (materiality + mechanism) / Trust · Conviction / Source link. End with a
"Watch, not act" section and "_Research only, not investment advice._"
"""

CHAT_SYSTEM = """You are a research assistant for a US-equities catalyst scanner. Answer the
user's question using ONLY the provided stored data (filings, ownership, news with source
links). Cite the source link for specifics. If the data lacks the answer, say so and suggest
a fresh pull. Research, not investment advice — never recommend buying or selling.
"""


def is_enabled(settings: dict[str, Any]) -> bool:
    """True only when the user has explicitly opted in via settings.yaml."""
    return settings.get("scoring", {}).get("mode") == "llm_api"


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
        model=model, max_tokens=8000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
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

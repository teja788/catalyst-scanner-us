"""FUTURE HOOK — notifications (email / Telegram). NOT IMPLEMENTED.

Section 17: push high-conviction signals as alerts once the background job can
pre-score (via scoring/llm_scorer.py in llm_api mode). OFF by default; the core
tool never calls this.

Design intent: read channel credentials from env vars ONLY inside send_alert
(no key at import); accept the same ranked-signal structure the scorer produces.
"""
from __future__ import annotations

from typing import Any


def send_alert(signals: list[dict[str, Any]], channel: str = "telegram") -> None:
    """Would push ranked signals to email/Telegram. Disabled."""
    raise NotImplementedError(
        "Notifications are a future hook. TODO: implement Telegram bot / SMTP email here; "
        "read TELEGRAM_BOT_TOKEN / SMTP_* from env inside this function only. Wire to the "
        "background job once llm_api scoring is enabled."
    )

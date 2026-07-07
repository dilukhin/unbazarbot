from __future__ import annotations

from typing import Iterable


def chunks(text: str, max_len: int = 3600) -> Iterable[str]:
    text = text or ""
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind(". ", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        yield text[:cut].strip()
        text = text[cut:].strip()
    if text:
        yield text


def user_label(user_id: int | None, username: str | None = None) -> str:
    if username:
        return f"@{username} ({user_id})" if user_id else f"@{username}"
    return str(user_id) if user_id else "unknown"

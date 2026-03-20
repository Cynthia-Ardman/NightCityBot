"""NightCityBot package setup."""

from __future__ import annotations

import discord

# Original send method reference for patching in tests
orig_send = discord.abc.Messageable.send


def _split_text(text: str, limit: int = 1900) -> list[str]:
    """Split *text* into chunks no longer than *limit*, breaking on newlines
    or spaces where possible to avoid cutting words mid-stream."""
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def _chunked_send(self: discord.abc.Messageable, content: str | None = None, **kwargs):
    """Send long messages in chunks, splitting on word/line boundaries."""
    if (
        isinstance(content, str)
        and len(content) > 1900
        and not kwargs.get("embed")
        and not kwargs.get("embeds")
    ):
        chunks = _split_text(content)
        # Only pass reference/mention_author on the first chunk
        first_kwargs = kwargs
        later_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in ("reference", "mention_author")
        }
        for i, chunk in enumerate(chunks):
            await orig_send(self, content=chunk, **(first_kwargs if i == 0 else later_kwargs))
        return
    await orig_send(self, content=content, **kwargs)


# Patch globally
discord.abc.Messageable.send = _chunked_send

__all__ = ["orig_send"]

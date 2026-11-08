"""Convert text formatting between Telegram and MAX (quotes, bold, etc.)."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram.enums import ParseMode
from aiogram.types import Message as TgMessage
from aiogram.utils.text_decorations import html_decoration

try:
    from maxapi.enums.format import Format as MaxFormat
except ImportError:  # pragma: no cover - older maxapi
    from maxapi.enums.parse_mode import TextFormat as MaxFormat

logger = logging.getLogger(__name__)

_FORMAT_TAG_RE = re.compile(
    r"<(blockquote|b|strong|i|em|u|ins|s|strike|del|code|pre|a|h1|mark)\b",
    re.I,
)


def telegram_rich_text(message: TgMessage) -> tuple[str, Any | None]:
    """
    Build text for MAX from a Telegram message.

    Returns (text, max_format). max_format is MaxFormat.HTML when entities exist.
    """
    if message.text:
        plain = message.text
        entities = message.entities or []
    elif message.caption:
        plain = message.caption
        entities = message.caption_entities or []
    else:
        return "", None

    if not entities:
        return plain, None

    rich = html_decoration.unparse(plain, entities)
    return adapt_html_tg_to_max(rich), MaxFormat.HTML


def max_body_rich_text(body) -> tuple[str, ParseMode | None]:
    """
    Build text for Telegram from a MAX message body.

    Returns (text, parse_mode). Uses HTML when markup/quotes are present.
    """
    if body is None:
        return "", None

    plain = body.text or ""
    markup = getattr(body, "markup", None) or []
    if not plain:
        return "", None
    if not markup:
        return plain, None

    rich_html = None
    try:
        rich_html = getattr(body, "html_text", None)
    except Exception as exc:
        logger.warning("html_text failed for MAX body: %s", exc)

    if not rich_html:
        try:
            decorated = getattr(body, "text_decorated", None)
            if decorated is not None:
                rich_html = decorated.as_html()
        except Exception as exc:
            logger.warning("text_decorated.as_html failed: %s", exc)

    if rich_html and _FORMAT_TAG_RE.search(rich_html):
        return adapt_html_max_to_tg(rich_html), ParseMode.HTML

    logger.debug(
        "MAX markup present (%d) but no usable HTML; styles=%s",
        len(markup),
        [getattr(m, "type", None) for m in markup],
    )
    return plain, None


def wrap_blockquote(text: str, *, is_html: bool) -> str:
    """Wrap text as a Telegram/MAX HTML blockquote."""
    inner = text if is_html else html.escape(text)
    stripped = inner.strip()
    if stripped.startswith("<blockquote>") and stripped.endswith("</blockquote>"):
        return stripped
    return f"<blockquote>{inner}</blockquote>"


def combine_html_parts(*parts: str) -> str:
    return "\n".join(part for part in parts if part and part.strip())


def split_telegram_text(
    text: str,
    parse_mode: ParseMode | None,
    *,
    limit: int = 4096,
) -> list[tuple[str, ParseMode | None]]:
    """
    Split text for Telegram message limit.

    If HTML would be cut mid-tag, fall back to plain text for safe chunking.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [(text, parse_mode)]

    working = text
    mode = parse_mode
    if mode is not None:
        # Avoid broken HTML across chunks.
        working = html.unescape(re.sub(r"<[^>]+>", "", text))
        mode = None
        logger.info(
            "Text exceeds %d chars with HTML; splitting as plain text (%d chars)",
            limit,
            len(working),
        )

    chunks: list[tuple[str, ParseMode | None]] = []
    remaining = working
    while remaining:
        if len(remaining) <= limit:
            chunks.append((remaining, mode))
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append((remaining[:cut].rstrip(), mode))
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def adapt_html_tg_to_max(rich: str) -> str:
    """Map Telegram HTML tags to what MAX accepts."""
    rich = rich.replace("<u>", "<ins>").replace("</u>", "</ins>")
    rich = re.sub(r"<blockquote\s+expandable\s*>", "<blockquote>", rich, flags=re.I)
    rich = re.sub(
        r'<span\s+class="tg-spoiler">(.*?)</span>',
        r"\1",
        rich,
        flags=re.I | re.S,
    )
    rich = re.sub(r"</?tg-spoiler>", "", rich, flags=re.I)
    rich = re.sub(r"<pre>\s*<code[^>]*>", "<code>", rich, flags=re.I)
    rich = re.sub(r"</code>\s*</pre>", "</code>", rich, flags=re.I)
    rich = re.sub(r"</?pre>", "", rich, flags=re.I)
    rich = re.sub(r"<tg-emoji[^>]*>|</tg-emoji>", "", rich, flags=re.I)
    return rich


def adapt_html_max_to_tg(rich: str) -> str:
    """Map MAX HTML tags to Telegram Bot API HTML."""
    rich = rich.replace("<ins>", "<u>").replace("</ins>", "</u>")
    rich = rich.replace("<h1>", "<b>").replace("</h1>", "</b>")
    rich = re.sub(r"</?mark>", "", rich, flags=re.I)
    rich = re.sub(
        r'href="max://user/(\d+)"',
        r'href="tg://user?id=\1"',
        rich,
        flags=re.I,
    )
    return rich

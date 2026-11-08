import html
import logging
import re
import shutil
import tempfile
from pathlib import Path

from aiogram.enums import ParseMode
from aiogram.types import (
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from maxapi import F
from maxapi.filters.channel_post import ChannelPostFilter
from maxapi.types import BotAdded, MessageCreated, MessageEdited, MessageRemoved
from maxapi.types.attachments.audio import Audio
from maxapi.types.attachments.file import File
from maxapi.types.attachments.image import Image
from maxapi.types.attachments.share import Share
from maxapi.types.attachments.video import Video

import config
import discovery
import linking
import message_map
from bridge_guard import content_fingerprint, fp_outbound, max_outbound, tg_outbound
from formatting import (
    combine_html_parts,
    max_body_rich_text,
    split_telegram_text,
    wrap_blockquote,
)
from forward_queue import forward_queue
from loader import max_dp, max_bot, tg_bot
from retry_util import retry_async

try:
    from maxapi.enums.message_link_type import MessageLinkType
except ImportError:  # pragma: no cover
    MessageLinkType = None  # type: ignore

logger = logging.getLogger(__name__)

TG_CAPTION_LIMIT = 1024


def _attachment_url(attachment) -> str | None:
    payload = getattr(attachment, "payload", None)
    url = getattr(payload, "url", None)
    if isinstance(url, str) and url:
        return url

    if isinstance(attachment, Video) and attachment.urls is not None:
        for attr in (
            "mp4_1080",
            "mp4_720",
            "mp4_480",
            "mp4_360",
            "mp4_240",
            "mp4_144",
        ):
            video_url = getattr(attachment.urls, attr, None)
            if video_url:
                return video_url
    return None


async def _resolve_attachment_url(attachment) -> str | None:
    url = _attachment_url(attachment)
    if url:
        return url

    if isinstance(attachment, Video) and attachment.token:
        try:
            video = await max_bot.get_video(attachment.token)
            return _attachment_url(video)
        except Exception as exc:
            logger.error("Failed to resolve MAX video token: %s", exc)
    return None


def _attachment_kind(attachment) -> str | None:
    if isinstance(attachment, Image):
        return "photo"
    if isinstance(attachment, Video):
        return "video"
    if isinstance(attachment, Audio):
        return "audio"
    if isinstance(attachment, File):
        return "document"
    if isinstance(attachment, Share):
        return "share"
    return None


def _mark_tg_messages(tg_channel_id: int, messages) -> list[int]:
    ids: list[int] = []
    if messages is None:
        return ids
    if not isinstance(messages, list):
        messages = [messages]
    for message in messages:
        tg_outbound.mark(f"{tg_channel_id}:{message.message_id}")
        ids.append(message.message_id)
    return ids


async def _download_max_attachments(
    attachments,
) -> tuple[list[tuple[str, Path]], Path | None]:
    downloaded: list[tuple[str, Path]] = []
    tmpdir: Path | None = None

    try:
        for attachment in attachments or []:
            kind = _attachment_kind(attachment)
            if kind is None:
                logger.warning(
                    "Unsupported MAX attachment type: %s",
                    getattr(attachment, "type", type(attachment).__name__),
                )
                continue
            if kind == "share":
                continue

            url = await _resolve_attachment_url(attachment)
            if not url:
                logger.warning("No download URL for MAX attachment type=%s", kind)
                continue

            if tmpdir is None:
                tmpdir = Path(tempfile.mkdtemp(prefix="max2tg_"))

            path = await max_bot.download_file(url, tmpdir)
            downloaded.append((kind, Path(path)))

        return downloaded, tmpdir
    except Exception:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise


async def _send_telegram_text(
    tg_channel_id: int,
    text: str,
    *,
    parse_mode=None,
) -> list[int]:
    ids: list[int] = []
    for index, (chunk, mode) in enumerate(
        split_telegram_text(text, parse_mode, limit=4096)
    ):
        kwargs = {}
        if mode is not None:
            kwargs["parse_mode"] = mode

        async def _send(chunk=chunk, kwargs=kwargs):
            return await tg_bot.send_message(
                chat_id=tg_channel_id,
                text=chunk,
                **kwargs,
            )

        try:
            sent = await retry_async(
                _send,
                attempts=3,
                base_delay=1.0,
                label=f"TG text {tg_channel_id}#{index}",
            )
        except Exception:
            if mode is None:
                raise
            plain = re.sub(r"<[^>]+>", "", chunk)
            sent = await tg_bot.send_message(chat_id=tg_channel_id, text=plain)
        ids.extend(_mark_tg_messages(tg_channel_id, sent))
    return ids


async def send_to_telegram(
    tg_channel_id: int,
    text: str,
    attachments,
    *,
    parse_mode=None,
) -> list[int]:
    """Forward MAX content to Telegram. Returns created Telegram message ids."""
    share_urls: list[str] = []
    for attachment in attachments or []:
        if isinstance(attachment, Share):
            payload = getattr(attachment, "payload", None)
            url = getattr(payload, "url", None)
            if url:
                share_urls.append(url)

    media: list[tuple[str, Path]] = []
    tmpdir: Path | None = None
    sent_ids: list[int] = []
    try:
        media, tmpdir = await _download_max_attachments(attachments)

        caption = text or None
        if share_urls:
            extra = "\n".join(share_urls)
            caption = f"{caption}\n{extra}".strip() if caption else extra

        if not caption and not media:
            logger.warning("Empty MAX post, nothing to forward")
            return []

        separate_text: str | None = None
        media_caption = caption
        text_kwargs: dict = {}
        if caption and parse_mode is not None:
            text_kwargs["parse_mode"] = parse_mode

        if media and caption and len(caption) > TG_CAPTION_LIMIT:
            logger.info(
                "Caption too long for Telegram (%d > %d), sending text separately",
                len(caption),
                TG_CAPTION_LIMIT,
            )
            separate_text = caption
            media_caption = None
            text_kwargs = {}

        if not media:
            sent_ids.extend(
                await _send_telegram_text(
                    tg_channel_id,
                    caption or "",
                    parse_mode=parse_mode,
                )
            )
            logger.info(
                "Forwarded text to Telegram %s (html=%s)",
                tg_channel_id,
                parse_mode is not None,
            )
            return sent_ids

        media_text_kwargs = text_kwargs if media_caption else {}

        async def _send_one(kind: str, path: Path, caption_value, kwargs):
            file = FSInputFile(path)
            if kind == "photo":
                return await tg_bot.send_photo(
                    chat_id=tg_channel_id,
                    photo=file,
                    caption=caption_value,
                    **kwargs,
                )
            if kind == "video":
                return await tg_bot.send_video(
                    chat_id=tg_channel_id,
                    video=file,
                    caption=caption_value,
                    **kwargs,
                )
            if kind == "audio":
                return await tg_bot.send_audio(
                    chat_id=tg_channel_id,
                    audio=file,
                    caption=caption_value,
                    **kwargs,
                )
            return await tg_bot.send_document(
                chat_id=tg_channel_id,
                document=file,
                caption=caption_value,
                **kwargs,
            )

        if len(media) == 1:
            kind, path = media[0]
            sent = await retry_async(
                lambda: _send_one(kind, path, media_caption, media_text_kwargs),
                attempts=3,
                base_delay=1.0,
                label=f"TG {kind} {tg_channel_id}",
            )
            sent_ids.extend(_mark_tg_messages(tg_channel_id, sent))
            logger.info("Forwarded %s to Telegram %s", kind, tg_channel_id)
        else:
            kinds = {kind for kind, _ in media}
            if kinds <= {"photo", "video"}:
                group = []
                for index, (kind, path) in enumerate(media):
                    file = FSInputFile(path)
                    item_caption = media_caption if index == 0 else None
                    item_kwargs = (
                        media_text_kwargs if index == 0 and item_caption else {}
                    )
                    cls = InputMediaPhoto if kind == "photo" else InputMediaVideo
                    group.append(cls(media=file, caption=item_caption, **item_kwargs))
                sent = await retry_async(
                    lambda: tg_bot.send_media_group(
                        chat_id=tg_channel_id, media=group
                    ),
                    attempts=3,
                    base_delay=1.0,
                    label=f"TG media_group {tg_channel_id}",
                )
                sent_ids.extend(_mark_tg_messages(tg_channel_id, sent))
            elif kinds == {"document"}:
                group = [
                    InputMediaDocument(
                        media=FSInputFile(path),
                        caption=media_caption if index == 0 else None,
                        **(media_text_kwargs if index == 0 and media_caption else {}),
                    )
                    for index, (_, path) in enumerate(media)
                ]
                sent = await retry_async(
                    lambda: tg_bot.send_media_group(
                        chat_id=tg_channel_id, media=group
                    ),
                    attempts=3,
                    base_delay=1.0,
                    label=f"TG docs {tg_channel_id}",
                )
                sent_ids.extend(_mark_tg_messages(tg_channel_id, sent))
            elif kinds == {"audio"}:
                group = [
                    InputMediaAudio(
                        media=FSInputFile(path),
                        caption=media_caption if index == 0 else None,
                        **(media_text_kwargs if index == 0 and media_caption else {}),
                    )
                    for index, (_, path) in enumerate(media)
                ]
                sent = await retry_async(
                    lambda: tg_bot.send_media_group(
                        chat_id=tg_channel_id, media=group
                    ),
                    attempts=3,
                    base_delay=1.0,
                    label=f"TG audio {tg_channel_id}",
                )
                sent_ids.extend(_mark_tg_messages(tg_channel_id, sent))
            else:
                for index, (kind, path) in enumerate(media):
                    item_caption = media_caption if index == 0 else None
                    item_kwargs = (
                        media_text_kwargs if index == 0 and item_caption else {}
                    )
                    sent = await retry_async(
                        lambda k=kind, p=path, c=item_caption, kw=item_kwargs: _send_one(
                            k, p, c, kw
                        ),
                        attempts=3,
                        base_delay=1.0,
                        label=f"TG mixed {kind}",
                    )
                    sent_ids.extend(_mark_tg_messages(tg_channel_id, sent))

        if separate_text:
            sent_ids.extend(
                await _send_telegram_text(
                    tg_channel_id,
                    separate_text,
                    parse_mode=parse_mode,
                )
            )
            logger.info("Forwarded long text separately to Telegram %s", tg_channel_id)
        return sent_ids
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


@max_dp.bot_added()
async def on_max_bot_added(event: BotAdded) -> None:
    chat_id = event.chat_id
    title = None
    chat = getattr(event, "chat", None)
    if chat is not None:
        title = getattr(chat, "title", None) or getattr(chat, "name", None)
    if title is None:
        try:
            fetched = await max_bot.get_chat_by_id(chat_id)
            title = getattr(fetched, "title", None) or getattr(fetched, "name", None)
        except Exception as exc:
            logger.warning("Could not fetch MAX chat %s title: %s", chat_id, exc)

    is_channel = bool(getattr(event, "is_channel", False))
    actor = event.user
    added_by = None
    added_by_name = None
    if actor is not None and not actor.is_bot:
        added_by = actor.user_id
        added_by_name = actor.full_name
    entry = discovery.register_max_channel(
        chat_id=chat_id,
        title=title,
        status="channel" if is_channel else "added",
        added_by=added_by,
        added_by_name=added_by_name,
    )
    logger.info(
        "Discovered MAX %s: title=%r id=%s paired=%s added_by=%s",
        "channel" if is_channel else "chat",
        entry.get("title"),
        entry["id"],
        entry["paired"],
        entry.get("added_by"),
    )
    if is_channel and not entry["paired"] and added_by is not None:
        await linking.notify_discovered("max", added_by, entry.get("title"))
    elif is_channel and not entry["paired"]:
        logger.info(
            "MAX channel %s has no adder id; linking uses a live admin check",
            entry["id"],
        )


@max_dp.bot_removed()
async def on_max_bot_removed(event) -> None:
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return
    discovery.mark_max_removed(chat_id)
    logger.info("Bot removed from MAX chat/channel id=%s", chat_id)


def _link_type_value(link) -> str | None:
    if link is None:
        return None
    link_type = getattr(link, "type", None)
    if link_type is None:
        return None
    return str(getattr(link_type, "value", link_type)).lower()


def _is_reply_link(link) -> bool:
    value = _link_type_value(link)
    if value == "reply":
        return True
    if MessageLinkType is not None and getattr(link, "type", None) == MessageLinkType.REPLY:
        return True
    return False


def _is_forward_link(link) -> bool:
    value = _link_type_value(link)
    if value == "forward":
        return True
    if MessageLinkType is not None and getattr(link, "type", None) == MessageLinkType.FORWARD:
        return True
    return False


async def _refresh_max_message(message):
    body = message.body
    if body is None or not body.mid:
        return message
    markup = getattr(body, "markup", None) or []
    if not (body.text or "").strip():
        return message
    if markup:
        return message

    try:
        fetched = await max_bot.get_message(body.mid)
    except Exception as exc:
        logger.warning("get_message(%s) failed: %s", body.mid, exc)
        return message

    if fetched is None:
        return message

    fetched_body = getattr(fetched, "body", None)
    fetched_markup = getattr(fetched_body, "markup", None) or []
    if fetched_markup:
        logger.info(
            "Refetched MAX message %s with markup=%d",
            body.mid,
            len(fetched_markup),
        )
        return fetched
    return message


def _extract_max_content(message) -> tuple[str | None, str, list, object | None]:
    body = message.body
    mid = body.mid if body else None
    body_text = (body.text if body else None) or ""
    attachments = list((body.attachments if body else None) or [])

    link = message.link
    linked = getattr(link, "message", None) if link else None

    parts: list[str] = []
    parse_mode = None

    if linked is not None and _is_reply_link(link):
        quoted_plain = (linked.text or "").strip()
        if quoted_plain:
            quoted_rich, quoted_mode = max_body_rich_text(linked)
            parts.append(
                wrap_blockquote(
                    quoted_rich or quoted_plain,
                    is_html=quoted_mode is not None,
                )
            )
            parse_mode = ParseMode.HTML
            logger.info(
                "Including MAX reply-quote (%d chars) from linked mid=%s",
                len(quoted_plain),
                getattr(linked, "mid", None),
            )
        if not attachments and linked.attachments:
            attachments = list(linked.attachments or [])

    source_body = body
    if (
        linked is not None
        and _is_forward_link(link)
        and (not body_text.strip())
        and not attachments
    ):
        attachments = list(linked.attachments or [])
        source_body = linked
        logger.info(
            "Using linked forward content (linked_mid=%s)",
            getattr(linked, "mid", None),
        )

    main_rich, main_mode = max_body_rich_text(source_body)
    main_plain = (source_body.text if source_body else None) or ""

    if main_rich or main_plain:
        if parse_mode is not None and main_mode is None:
            parts.append(html.escape(main_plain))
        else:
            parts.append(main_rich or main_plain)
            if main_mode is not None:
                parse_mode = main_mode

    text = combine_html_parts(*parts) if parts else ""
    return mid, text, attachments, parse_mode


async def _forward_max_post(
    max_channel_id: int,
    tg_channel_id: int,
    message,
    *,
    replace_existing: bool = False,
) -> None:
    message = await _refresh_max_message(message)
    mid, text, attachments, parse_mode = _extract_max_content(message)

    logger.info(
        "MAX %s → TG %s mid=%s text=%s html=%s attachments=%d link=%s replace=%s",
        max_channel_id,
        tg_channel_id,
        mid,
        bool(text.strip()),
        parse_mode is not None,
        len(attachments),
        _link_type_value(message.link),
        replace_existing,
    )

    if mid and max_outbound.seen(f"{max_channel_id}:{mid}"):
        logger.info("Skip echo from our own MAX post mid=%s", mid)
        return

    plain_for_fp = ""
    body = message.body
    if body and body.text:
        plain_for_fp = body.text
    elif text:
        plain_for_fp = re.sub(r"<[^>]+>", "", text)
    fp = content_fingerprint(plain_for_fp, attachments)
    if (not replace_existing) and fp_outbound.consume(f"{max_channel_id}:fp:{fp}"):
        logger.info("Skip echo by fingerprint mid=%s", mid)
        return

    if replace_existing and mid:
        prev = message_map.unlink_by_max(max_channel_id, mid)
        if prev:
            for tg_message_id in prev.get("tg_message_ids") or []:
                try:
                    await retry_async(
                        lambda mid_=tg_message_id: tg_bot.delete_message(
                            chat_id=tg_channel_id,
                            message_id=mid_,
                        ),
                        attempts=3,
                        base_delay=0.5,
                        label=f"TG delete {tg_message_id}",
                    )
                    tg_outbound.mark(f"{tg_channel_id}:{tg_message_id}")
                except Exception as exc:
                    logger.warning(
                        "Failed to delete old TG message %s: %s",
                        tg_message_id,
                        exc,
                    )

    tg_ids = await send_to_telegram(
        tg_channel_id,
        text,
        attachments,
        parse_mode=parse_mode,
    )
    if mid and tg_ids:
        message_map.link_pair(
            tg_chat_id=tg_channel_id,
            tg_message_ids=tg_ids,
            max_chat_id=max_channel_id,
            max_mid=mid,
        )


@max_dp.message_created(
    ChannelPostFilter(),
    F.message.recipient.chat_id.in_(config.MAX_CHANNEL_IDS),
)
async def on_max_channel_post(event: MessageCreated) -> None:
    max_channel_id = event.message.recipient.chat_id
    tg_channel_id = config.MAX_TO_TG.get(max_channel_id)
    if tg_channel_id is None:
        return

    forward_queue.submit(
        _forward_max_post(max_channel_id, tg_channel_id, event.message),
        name=f"max2tg-{max_channel_id}",
    )


@max_dp.message_edited(
    ChannelPostFilter(),
    F.message.recipient.chat_id.in_(config.MAX_CHANNEL_IDS),
)
async def on_max_channel_edited(event: MessageEdited) -> None:
    max_channel_id = event.message.recipient.chat_id
    tg_channel_id = config.MAX_TO_TG.get(max_channel_id)
    if tg_channel_id is None:
        return

    mid = event.message.body.mid if event.message.body else None
    logger.info("Edited MAX post %s — resync to TG %s", mid, tg_channel_id)
    forward_queue.submit(
        _forward_max_post(
            max_channel_id,
            tg_channel_id,
            event.message,
            replace_existing=True,
        ),
        name=f"max-edit-{mid}",
    )


@max_dp.message_removed()
async def on_max_message_removed(event: MessageRemoved) -> None:
    max_channel_id = event.chat_id
    if max_channel_id not in config.MAX_CHANNEL_IDS:
        return
    mid = event.message_id
    tg_channel_id = config.MAX_TO_TG.get(max_channel_id)
    if tg_channel_id is None:
        return

    mapping = message_map.unlink_by_max(max_channel_id, mid)
    if not mapping:
        logger.info("No TG mapping for deleted MAX mid=%s", mid)
        return

    async def _delete_all() -> None:
        for tg_message_id in mapping.get("tg_message_ids") or []:
            try:
                await retry_async(
                    lambda mid_=tg_message_id: tg_bot.delete_message(
                        chat_id=tg_channel_id,
                        message_id=mid_,
                    ),
                    attempts=3,
                    base_delay=0.5,
                    label=f"TG delete {tg_message_id}",
                )
                tg_outbound.mark(f"{tg_channel_id}:{tg_message_id}")
            except Exception as exc:
                logger.warning(
                    "Failed to delete TG message %s for MAX %s: %s",
                    tg_message_id,
                    mid,
                    exc,
                )
        logger.info("Deleted TG mirrors for MAX mid=%s", mid)

    forward_queue.submit(_delete_all(), name=f"max-del-{mid}")

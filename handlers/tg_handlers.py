import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import F
from aiogram.enums import ChatType
from aiogram.types import ChatMemberUpdated, Message as TgMessage
from maxapi.types import InputMedia

import config
import discovery
import linking
import message_map
from bridge_guard import content_fingerprint, fp_outbound, max_outbound, tg_outbound
from formatting import telegram_rich_text
from forward_queue import forward_queue
from loader import tg_dp, tg_bot, max_bot
from retry_util import retry_async

logger = logging.getLogger(__name__)

ALBUM_DEBOUNCE_SEC = 1.5


@dataclass
class AlbumBuffer:
    max_channel_id: int
    messages: list[TgMessage] = field(default_factory=list)
    version: int = 0


media_groups: dict[str, AlbumBuffer] = {}


def _album_key(tg_chat_id: int, media_group_id: str) -> str:
    return f"{tg_chat_id}:{media_group_id}"


def _message_text(message: TgMessage) -> str:
    return message.text or message.caption or ""


def _file_suffix(message: TgMessage) -> str | None:
    if message.photo:
        return ".jpg"
    if message.video:
        return Path(message.video.file_name or "video.mp4").suffix or ".mp4"
    if message.animation:
        return Path(message.animation.file_name or "animation.mp4").suffix or ".mp4"
    if message.document:
        name = message.document.file_name or "file.bin"
        return Path(name).suffix or ".bin"
    if message.audio:
        return Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
    if message.voice:
        return ".ogg"
    return None


def _telegram_file_id(message: TgMessage) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.video:
        return message.video.file_id
    if message.animation:
        return message.animation.file_id
    if message.document:
        return message.document.file_id
    if message.audio:
        return message.audio.file_id
    if message.voice:
        return message.voice.file_id
    return None


def _unsupported_kind(message: TgMessage) -> str:
    if message.sticker:
        return "sticker"
    if message.video_note:
        return "video_note"
    if message.contact:
        return "contact"
    if message.location or message.venue:
        return "location"
    if message.poll:
        return "poll"
    return "unknown"


async def _download_attachment(message: TgMessage) -> str | None:
    file_id = _telegram_file_id(message)
    suffix = _file_suffix(message)
    if not file_id or not suffix:
        return None

    file_info = await tg_bot.get_file(file_id)
    if not file_info.file_path:
        raise RuntimeError(f"Telegram file_path is empty for file_id={file_id}")

    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        await tg_bot.download_file(file_info.file_path, destination=temp_path)
        return temp_path
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


async def _mark_max_result(max_channel_id: int, result) -> str | None:
    if result is None:
        logger.warning(
            "send_message to MAX %s returned empty result; echo guard may be weak",
            max_channel_id,
        )
        return None

    message = getattr(result, "message", None)
    body = getattr(message, "body", None)
    mid = getattr(body, "mid", None)
    if mid:
        max_outbound.mark(f"{max_channel_id}:{mid}")
    else:
        logger.warning("send_message to MAX %s returned no mid", max_channel_id)
        return None

    text = (getattr(body, "text", None) if body else None) or ""
    attachments = list((getattr(body, "attachments", None) if body else None) or [])

    # Strengthen fingerprint: refetch if attachments missing in send response.
    if mid and not attachments:
        try:
            fetched = await max_bot.get_message(mid)
            fbody = getattr(fetched, "body", None)
            if fbody is not None:
                text = fbody.text or text
                attachments = list(fbody.attachments or [])
        except Exception as exc:
            logger.debug("Fingerprint refetch failed for %s: %s", mid, exc)

    if text.strip() or attachments:
        fp_outbound.mark(
            f"{max_channel_id}:fp:{content_fingerprint(text, attachments)}"
        )
    return mid


async def send_to_max(
    max_channel_id: int,
    text: str,
    messages: list[TgMessage],
    *,
    text_format=None,
    edit_mid: str | None = None,
) -> str | None:
    """Download media and send/edit a MAX channel post. Returns mid."""
    temp_files: list[str] = []
    attachments: list[InputMedia] = []

    try:
        for message in messages:
            path = await _download_attachment(message)
            if path:
                temp_files.append(path)
                attachments.append(InputMedia(path))

        if not text and not attachments and not edit_mid:
            kinds = [_unsupported_kind(m) for m in messages]
            logger.warning("Unsupported Telegram content, skip: %s", ", ".join(kinds))
            return None

        send_kwargs = {
            "text": text or None,
            "attachments": attachments or None,
        }
        if text and text_format is not None:
            send_kwargs["format"] = text_format

        async def _do_send():
            if edit_mid:
                return await max_bot.edit_message(message_id=edit_mid, **send_kwargs)
            return await max_bot.send_message(chat_id=max_channel_id, **send_kwargs)

        result = await retry_async(
            _do_send,
            attempts=3,
            base_delay=1.0,
            label=f"MAX {'edit' if edit_mid else 'send'} {max_channel_id}",
        )

        if edit_mid:
            max_outbound.mark(f"{max_channel_id}:{edit_mid}")
            logger.info("Edited MAX message %s", edit_mid)
            return edit_mid

        mid = await _mark_max_result(max_channel_id, result)
        logger.info(
            "Forwarded TG → MAX %s (text=%s, html=%s, attachments=%d, mid=%s)",
            max_channel_id,
            bool(text),
            text_format is not None,
            len(attachments),
            mid,
        )
        return mid
    finally:
        for path in temp_files:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.error("Error removing temp file %s: %s", path, exc)


async def _flush_album(album_key: str, version: int) -> None:
    buffer = media_groups.get(album_key)
    if buffer is None or buffer.version != version:
        return

    media_groups.pop(album_key, None)
    messages = sorted(buffer.messages, key=lambda m: m.message_id)
    text = ""
    text_format = None
    for message in messages:
        rich, fmt = telegram_rich_text(message)
        if rich:
            text = rich
            text_format = fmt
            break

    max_channel_id = buffer.max_channel_id
    tg_chat_id = messages[0].chat.id

    async def _send_album() -> None:
        mid = await send_to_max(
            max_channel_id,
            text,
            messages,
            text_format=text_format,
        )
        if mid:
            message_map.link_pair(
                tg_chat_id=tg_chat_id,
                tg_message_ids=[m.message_id for m in messages],
                max_chat_id=max_channel_id,
                max_mid=mid,
            )
        logger.info(
            "Forwarded media group %s (%d items) to MAX %s",
            album_key,
            len(messages),
            max_channel_id,
        )

    forward_queue.submit(_send_album(), name=f"tg-album-{album_key}")


async def _album_debounce(album_key: str, version: int) -> None:
    await asyncio.sleep(ALBUM_DEBOUNCE_SEC)
    await _flush_album(album_key, version)


async def _forward_tg_post(message: TgMessage, max_channel_id: int) -> None:
    text = _message_text(message)
    rich, text_format = telegram_rich_text(message)
    mid = await send_to_max(
        max_channel_id,
        rich or text,
        [message],
        text_format=text_format,
    )
    if mid:
        message_map.link_pair(
            tg_chat_id=message.chat.id,
            tg_message_ids=[message.message_id],
            max_chat_id=max_channel_id,
            max_mid=mid,
        )


async def _edit_max_from_tg(message: TgMessage, max_channel_id: int, max_mid: str) -> None:
    text = _message_text(message)
    rich, text_format = telegram_rich_text(message)
    await send_to_max(
        max_channel_id,
        rich or text,
        [message],
        text_format=text_format,
        edit_mid=max_mid,
    )


@tg_dp.channel_post(F.chat.id.in_(config.TG_CHANNEL_IDS))
async def on_channel_post(message: TgMessage) -> None:
    """Forward new Telegram channel posts to the paired MAX channel."""
    tg_chat_id = message.chat.id
    max_channel_id = config.TG_TO_MAX.get(tg_chat_id)
    if max_channel_id is None:
        return

    logger.info(
        "New post in Telegram channel %s → MAX %s: %s",
        tg_chat_id,
        max_channel_id,
        message.message_id,
    )

    if tg_outbound.seen(f"{tg_chat_id}:{message.message_id}"):
        logger.info("Skip echo from our own Telegram post %s", message.message_id)
        return

    if message.media_group_id:
        album_key = _album_key(tg_chat_id, message.media_group_id)
        buffer = media_groups.get(album_key)
        if buffer is None:
            buffer = AlbumBuffer(max_channel_id=max_channel_id)
            media_groups[album_key] = buffer

        buffer.messages.append(message)
        buffer.version += 1
        asyncio.create_task(
            _album_debounce(album_key, buffer.version),
            name=f"album-{album_key}-{buffer.version}",
        )
        return

    forward_queue.submit(
        _forward_tg_post(message, max_channel_id),
        name=f"tg2max-{tg_chat_id}-{message.message_id}",
    )


@tg_dp.edited_channel_post(F.chat.id.in_(config.TG_CHANNEL_IDS))
async def on_edited_channel_post(message: TgMessage) -> None:
    """Propagate Telegram channel edits to the mapped MAX message."""
    tg_chat_id = message.chat.id
    max_channel_id = config.TG_TO_MAX.get(tg_chat_id)
    if max_channel_id is None:
        return

    if tg_outbound.seen(f"{tg_chat_id}:{message.message_id}"):
        return

    mapping = message_map.get_max_for_tg(tg_chat_id, message.message_id)
    if not mapping:
        logger.info(
            "No MAX mapping for edited TG post %s:%s — skip",
            tg_chat_id,
            message.message_id,
        )
        return

    max_mid = mapping["max_mid"]
    logger.info(
        "Edited TG post %s:%s → edit MAX %s",
        tg_chat_id,
        message.message_id,
        max_mid,
    )
    forward_queue.submit(
        _edit_max_from_tg(message, max_channel_id, max_mid),
        name=f"tg-edit-{message.message_id}",
    )


_ADMIN_STATUSES = {"administrator", "creator"}
_GONE_STATUSES = {"left", "kicked", "restricted"}


@tg_dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    """Auto-capture Telegram channel id/title when the bot is added as admin."""
    chat = event.chat
    if chat.type != ChatType.CHANNEL:
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    if new_status in _ADMIN_STATUSES and old_status not in _ADMIN_STATUSES:
        actor = event.from_user
        added_by = None
        added_by_name = None
        if actor is not None and not actor.is_bot:
            added_by = actor.id
            added_by_name = actor.full_name
        entry = discovery.register_telegram_channel(
            chat_id=chat.id,
            title=chat.title,
            username=chat.username,
            status=str(getattr(new_status, "value", new_status)),
            added_by=added_by,
            added_by_name=added_by_name,
        )
        paired = entry["paired"]
        logger.info(
            "Discovered Telegram channel: title=%r username=%s id=%s paired=%s added_by=%s",
            entry.get("title"),
            f"@{entry['username']}" if entry.get("username") else None,
            entry["id"],
            paired,
            entry.get("added_by"),
        )
        if not paired and added_by is not None:
            await linking.notify_discovered("tg", added_by, entry.get("title"))
        return

    if new_status in _GONE_STATUSES and old_status in _ADMIN_STATUSES:
        discovery.mark_telegram_removed(chat.id)
        logger.info(
            "Bot removed from Telegram channel id=%s title=%r",
            chat.id,
            chat.title,
        )

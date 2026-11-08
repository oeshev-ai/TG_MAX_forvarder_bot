"""MAX private dialog: issue a link code, redeem one, list and unlink pairs."""

from __future__ import annotations

import logging

from maxapi import F
from maxapi.enums.chat_type import ChatType
from maxapi.filters.command import Command
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import claims
import linking
from linking import Screen
from loader import max_bot, max_dp

logger = logging.getLogger(__name__)

_DIALOG = F.message.recipient.chat_type == ChatType.DIALOG


def _markup(screen: Screen) -> list | None:
    if not screen.buttons:
        return None
    builder = InlineKeyboardBuilder()
    for button in screen.buttons:
        if button.url:
            builder.row(LinkButton(text=button.text, url=button.url))
        elif button.payload:
            builder.row(CallbackButton(text=button.text, payload=button.payload))
    return [builder.as_markup()]


async def send_screen(chat_id: int | None, user_id: int | None, screen: Screen) -> None:
    await max_bot.send_message(
        chat_id=chat_id,
        user_id=None if chat_id is not None else user_id,
        text=screen.text,
        attachments=_markup(screen),
    )


async def _show_callback(event: MessageCallback, screen: Screen) -> None:
    attachments = _markup(screen)
    try:
        await event.answer(
            new_text=screen.text,
            attachments=[] if attachments is None else attachments,
        )
        return
    except Exception as exc:
        logger.info("MAX callback edit failed: %s", exc)
    message = event.message
    chat_id = None if message is None else message.recipient.chat_id
    user_id = event.callback.user.user_id
    await send_screen(chat_id, user_id, screen)


def _sender_id(event: MessageCreated) -> int | None:
    sender = event.message.sender
    if sender is None or sender.is_bot:
        return None
    return sender.user_id


def _dialog_chat_id(event: MessageCreated) -> int | None:
    return event.message.recipient.chat_id


def _is_code_text(text: str | None) -> bool:
    return bool(text) and claims.looks_like_code(text)


@max_dp.bot_started()
async def on_bot_started(event: BotStarted) -> None:
    user = event.user
    if user is None or user.is_bot:
        return
    payload = (event.payload or "").strip()
    logger.info("MAX bot started chat_id=%s", event.chat_id)
    if payload:
        screen = await linking.redeem("max", user.user_id, payload)
    else:
        screen = await linking.menu("max", user.user_id)
    await send_screen(event.chat_id, user.user_id, screen)


@max_dp.message_created(Command("start"), _DIALOG)
async def on_start(event: MessageCreated, args: list[str]) -> None:
    user_id = _sender_id(event)
    if user_id is None:
        return
    if args:
        screen = await linking.redeem("max", user_id, args[0])
    else:
        screen = await linking.menu("max", user_id)
    await send_screen(_dialog_chat_id(event), user_id, screen)


@max_dp.message_created(Command("link"), _DIALOG)
async def on_link(event: MessageCreated) -> None:
    user_id = _sender_id(event)
    if user_id is None:
        return
    await send_screen(_dialog_chat_id(event), user_id, await linking.menu("max", user_id))


@max_dp.message_created(Command("channels"), _DIALOG)
async def on_channels(event: MessageCreated) -> None:
    user_id = _sender_id(event)
    if user_id is None:
        return
    screen = await linking.channels_screen("max", user_id)
    await send_screen(_dialog_chat_id(event), user_id, screen)


@max_dp.message_created(Command("unlink"), _DIALOG)
async def on_unlink(event: MessageCreated) -> None:
    user_id = _sender_id(event)
    if user_id is None:
        return
    screen = await linking.unlink_menu("max", user_id)
    await send_screen(_dialog_chat_id(event), user_id, screen)


@max_dp.message_created(_DIALOG, F.message.body.text.func(_is_code_text))
async def on_code(event: MessageCreated) -> None:
    user_id = _sender_id(event)
    body = event.message.body
    if user_id is None or body is None or not body.text:
        return
    screen = await linking.redeem("max", user_id, body.text)
    await send_screen(_dialog_chat_id(event), user_id, screen)


@max_dp.message_callback()
async def on_callback(event: MessageCallback) -> None:
    payload = event.callback.payload or ""
    if not payload.startswith("lnk:"):
        await event.ack()
        return
    user = event.callback.user
    if user.is_bot:
        await event.ack()
        return
    message = event.message
    if message is not None and message.recipient.chat_type != ChatType.DIALOG:
        await event.ack()
        return
    screen = await linking.handle_payload("max", user.user_id, payload)
    await _show_callback(event, screen)

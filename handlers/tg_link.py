"""Telegram private chat: issue a link code, redeem one, list and unlink pairs."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import claims
import linking
from linking import Screen
from loader import tg_dp

logger = logging.getLogger(__name__)

_PRIVATE = F.chat.type == ChatType.PRIVATE


def _markup(screen: Screen) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for button in screen.buttons:
        if button.url:
            rows.append([InlineKeyboardButton(text=button.text, url=button.url)])
        elif button.payload:
            rows.append(
                [InlineKeyboardButton(text=button.text, callback_data=button.payload)]
            )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send(message: Message, screen: Screen) -> None:
    await message.answer(screen.text, reply_markup=_markup(screen))


async def _edit(query: CallbackQuery, screen: Screen) -> None:
    markup = _markup(screen)
    message = query.message
    if message is not None:
        try:
            await message.edit_text(screen.text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.info("Telegram callback edit failed: %s", exc)
                await message.answer(screen.text, reply_markup=markup)
    await query.answer()


def _is_code(message: Message) -> bool:
    return bool(message.text) and claims.looks_like_code(message.text)


@tg_dp.message(CommandStart(), _PRIVATE)
async def on_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None:
        return
    payload = (command.args or "").strip()
    if payload:
        screen = await linking.redeem("tg", user.id, payload.split()[0])
    else:
        screen = await linking.menu("tg", user.id)
    await _send(message, screen)


@tg_dp.message(Command("link"), _PRIVATE)
async def on_link(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    await _send(message, await linking.menu("tg", user.id))


@tg_dp.message(Command("channels"), _PRIVATE)
async def on_channels(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    await _send(message, await linking.channels_screen("tg", user.id))


@tg_dp.message(Command("unlink"), _PRIVATE)
async def on_unlink(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    await _send(message, await linking.unlink_menu("tg", user.id))


@tg_dp.message(_PRIVATE, F.func(_is_code))
async def on_code(message: Message) -> None:
    user = message.from_user
    if user is None or not message.text:
        return
    await _send(message, await linking.redeem("tg", user.id, message.text))


@tg_dp.callback_query(F.data.startswith("lnk:"))
async def on_callback(query: CallbackQuery) -> None:
    message = query.message
    if message is None or message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    user = query.from_user
    screen = await linking.handle_payload("tg", user.id, query.data or "")
    await _edit(query, screen)

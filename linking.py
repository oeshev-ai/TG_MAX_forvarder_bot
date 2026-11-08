"""Private-chat wizard that links a Telegram channel with a MAX channel.

A short code is issued in one private chat and redeemed in the other.
Telegram and MAX user ids are never compared: possession of the code
plus admin rights on the chosen channel is the check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram.enums import ChatMemberStatus
from aiogram.types import BotCommand as TgBotCommand

import claims
import config
import discovery
import pairs


def _tg_bot():
    from loader import tg_bot

    return tg_bot


def _max_bot():
    from loader import max_bot

    return max_bot

logger = logging.getLogger(__name__)

OTHER = {"tg": "max", "max": "tg"}
PLATFORM_NAME = {"tg": "Telegram", "max": "MAX"}

tg_bot_username: str | None = None
max_bot_username: str | None = None


@dataclass
class Button:
    text: str
    payload: str | None = None
    url: str | None = None


@dataclass
class Screen:
    text: str
    buttons: list[Button] = field(default_factory=list)


def _menu_button() -> Button:
    return Button("Меню", payload="lnk:home")


def _with_menu(screen: Screen) -> Screen:
    screen.buttons.append(_menu_button())
    return screen


def channel_label(entry: dict | None, fallback: int) -> str:
    if not entry:
        return str(fallback)
    title = str(entry.get("title") or entry.get("id") or fallback).replace("\n", " ").strip()
    username = entry.get("username")
    if username:
        title = f"{title} (@{username})"
    return title or str(fallback)


def _button_text(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _split_payload(payload: str) -> tuple[str, str | None]:
    rest = payload[4:] if payload.startswith("lnk:") else payload
    if ":" in rest:
        action, arg = rest.split(":", 1)
        return action, arg
    return rest, None


def _parse_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def remember_bots() -> None:
    """Cache usernames for deep links and publish the private-chat commands."""
    global tg_bot_username, max_bot_username
    try:
        me = await _tg_bot().get_me()
        tg_bot_username = (me.username or "").lstrip("@") or None
    except Exception as exc:
        logger.warning("Could not read Telegram bot username: %s", exc)
    try:
        me = await _max_bot().get_me()
        max_bot_username = (getattr(me, "username", None) or "").lstrip("@") or None
    except Exception as exc:
        logger.warning("Could not read MAX bot username: %s", exc)

    tg_commands = [
        TgBotCommand(command="start", description="Список каналов и код привязки"),
        TgBotCommand(command="link", description="Получить код привязки"),
        TgBotCommand(command="channels", description="Показать связанные каналы"),
        TgBotCommand(command="unlink", description="Снять связь каналов"),
    ]
    try:
        await _tg_bot().set_my_commands(tg_commands)
    except Exception as exc:
        logger.warning("Could not set Telegram commands: %s", exc)
    try:
        from maxapi.types.command import BotCommand as MaxBotCommand

        await _max_bot().set_commands(
            MaxBotCommand(name="start", description="Список каналов и код привязки"),
            MaxBotCommand(name="link", description="Получить код привязки"),
            MaxBotCommand(name="channels", description="Показать связанные каналы"),
            MaxBotCommand(name="unlink", description="Снять связь каналов"),
        )
    except Exception as exc:
        logger.warning("Could not set MAX commands: %s", exc)


async def notify_discovered(platform: str, user_id: int, title: str | None) -> None:
    name = title or "канал"
    other = PLATFORM_NAME[OTHER[platform]]
    text = (
        f"Канал «{name}» найден.\n\n"
        f"Напишите /link здесь, чтобы получить код, и отправьте его боту в {other}."
    )
    try:
        if platform == "tg":
            await _tg_bot().send_message(user_id, text)
        else:
            await _max_bot().send_message(user_id=user_id, text=text)
    except Exception as exc:
        logger.info(
            "Could not message %s user %s about a new channel: %s",
            platform,
            user_id,
            exc,
        )


async def _api_is_admin(platform: str, user_id: int, channel_id: int) -> bool | None:
    """True/False when the API answered, None when the check is inconclusive."""
    if platform == "tg":
        try:
            member = await _tg_bot().get_chat_member(channel_id, user_id)
        except Exception as exc:
            logger.info(
                "Telegram admin check failed chat=%s user=%s: %s",
                channel_id,
                user_id,
                exc,
            )
            return None
        status = member.status
        if status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            return True
        return False

    try:
        member = await _max_bot().get_chat_member(channel_id, user_id)
    except Exception as exc:
        logger.info(
            "MAX admin check failed chat=%s user=%s: %s",
            channel_id,
            user_id,
            exc,
        )
        return None
    if member is None:
        return None
    if member.is_owner or member.is_admin:
        return True
    if member.is_owner is False and member.is_admin is False:
        return False
    return None


async def user_can_manage(platform: str, user_id: int, channel_id: int) -> bool:
    entry = discovery.get_channel(platform, channel_id)
    paired_ids = config.TG_CHANNEL_IDS if platform == "tg" else config.MAX_CHANNEL_IDS
    is_paired = channel_id in paired_ids
    linkable = discovery.is_linkable(entry, platform)
    # Paired channels may lack discovery (env import) or be marked removed;
    # still allow a live admin check so /channels and /unlink keep working.
    if entry is None:
        if not is_paired:
            return False
        verdict = await _api_is_admin(platform, user_id, channel_id)
        return bool(verdict)
    if not linkable and not is_paired:
        return False
    verdict = await _api_is_admin(platform, user_id, channel_id)
    if verdict is not None:
        return verdict
    return entry.get("added_by") == user_id


async def _manageable(home: str, user_id: int, *, unpaired_only: bool) -> list[dict]:
    found: list[dict] = []
    paired_ids = config.TG_CHANNEL_IDS if home == "tg" else config.MAX_CHANNEL_IDS
    for entry in discovery.list_linkable(home):
        channel_id = int(entry["id"])
        if unpaired_only and channel_id in paired_ids:
            continue
        if await user_can_manage(home, user_id, channel_id):
            found.append(entry)
    return found


def _blocked_screen() -> Screen:
    return _with_menu(
        Screen("Слишком много неверных кодов. Подождите 10 минут и запросите код заново.")
    )


def _missing_pending() -> Screen:
    return _with_menu(Screen("Нет активной привязки. Запросите код заново."))


async def menu(home: str, user_id: int) -> Screen:
    other = PLATFORM_NAME[OTHER[home]]
    channels = await _manageable(home, user_id, unpaired_only=True)
    lines = [
        "Я синхронизирую посты между каналами Telegram и MAX.",
        "",
        f"Добавьте меня администратором в канал здесь и в {other}.",
        "Кнопка канала выдаёт код на 10 минут. Отправьте его боту на другой стороне.",
        "Если код уже есть, просто пришлите его в этот чат.",
        "",
        "/channels — текущие пары",
        "/unlink — снять связь",
    ]
    if not channels:
        lines.extend(["", "Свободных каналов, где вы админ, пока нет."])
    buttons = [
        Button(
            _button_text(f"Код: {channel_label(entry, int(entry['id']))}"),
            payload=f"lnk:i:{entry['id']}",
        )
        for entry in channels
    ]
    return Screen("\n".join(lines), buttons)


async def channels_screen(home: str, user_id: int) -> Screen:
    linked: list[str] = []
    for pair in pairs.list_pairs():
        home_id = pair["tg_id"] if home == "tg" else pair["max_id"]
        if not await user_can_manage(home, user_id, home_id):
            continue
        tg_label = channel_label(discovery.get_channel("tg", pair["tg_id"]), pair["tg_id"])
        max_label = channel_label(discovery.get_channel("max", pair["max_id"]), pair["max_id"])
        linked.append(f"• {tg_label} ↔ {max_label}")
    free = await _manageable(home, user_id, unpaired_only=True)
    lines: list[str] = []
    if linked:
        lines.append("Связаны:")
        lines.extend(linked)
    else:
        lines.append("Связанных каналов, где вы админ, нет.")
    if free:
        lines.append("")
        lines.append("Без пары:")
        lines.extend(
            f"• {channel_label(entry, int(entry['id']))}" for entry in free
        )
    return _with_menu(Screen("\n".join(lines)))


async def issue(home: str, user_id: int, channel_id: int) -> Screen:
    if not await user_can_manage(home, user_id, channel_id):
        return _with_menu(Screen("Этот канал вам недоступен."))
    if channel_id in (config.TG_CHANNEL_IDS if home == "tg" else config.MAX_CHANNEL_IDS):
        return _with_menu(Screen("Канал уже связан. Сначала снимите связь через /unlink."))
    code = claims.issue(home, channel_id, user_id)
    entry = discovery.get_channel(home, channel_id)
    label = channel_label(entry, channel_id)
    other = PLATFORM_NAME[OTHER[home]]
    lines = [
        f"Код {claims.pretty(code)} для канала «{label}».",
        "",
        f"Отправьте его боту в {other} в течение 10 минут. "
        "Код одноразовый. В канал его отправлять не нужно.",
    ]
    buttons: list[Button] = []
    url = _deep_link(home, code)
    if url:
        buttons.append(Button(f"Открыть бота в {other}", url=url))
    logger.info("Issued link code for %s channel %s", home, channel_id)
    return Screen("\n".join(lines), buttons)


def _deep_link(issued_on: str, code: str) -> str | None:
    if issued_on == "tg":
        if not max_bot_username:
            return None
        try:
            from maxapi.utils.deep_linking import create_start_link

            return create_start_link(max_bot_username, code)
        except ValueError:
            return None
    if not tg_bot_username:
        return None
    return f"https://t.me/{tg_bot_username}?start={code}"


async def redeem(home: str, user_id: int, raw: str) -> Screen:
    if claims.is_blocked(home, user_id):
        return _blocked_screen()
    pending = claims.get_pending(home, user_id)
    if pending is not None and claims.normalize(raw) == pending.claim.code:
        if pending.chosen_id is not None:
            return await _confirm_screen(home, user_id)
        channels = await _manageable(home, user_id, unpaired_only=True)
        if not channels:
            return _with_menu(
                Screen(
                    f"Код ещё действует. Сначала добавьте меня администратором "
                    f"в канал {PLATFORM_NAME[home]} и пришлите код снова."
                )
            )
        return _pick_screen(home, pending.claim, channels)
    if not claims.looks_like_code(raw):
        return _with_menu(
            Screen("Не вижу кода. Пришлите 8 символов, которые выдал бот на другой стороне.")
        )
    claim = claims.lookup(raw)
    if claim is None:
        claims.note_failure(home, user_id)
        return _with_menu(Screen("Код не найден или истёк. Запросите новый на другой стороне."))
    if claim.side == home:
        where = PLATFORM_NAME[OTHER[home]]
        return _with_menu(Screen(f"Этот код нужно отправить боту в {where}."))
    if not await user_can_manage(claim.side, claim.issuer_user_id, claim.channel_id):
        return _with_menu(
            Screen("Права на исходный канал уже не подтверждаются. Запросите новый код.")
        )
    channels = await _manageable(home, user_id, unpaired_only=True)
    if not channels:
        other = PLATFORM_NAME[home]
        return _with_menu(
            Screen(
                f"Код ещё действует. Сначала добавьте меня администратором в канал {other} "
                "и пришлите код снова."
            )
        )
    chosen = int(channels[0]["id"]) if len(channels) == 1 else None
    if claims.hold(raw, home, user_id, chosen) is None:
        return _with_menu(Screen("Код не найден или истёк. Запросите новый на другой стороне."))
    if chosen is None:
        return _pick_screen(home, claim, channels)
    return await _confirm_screen(home, user_id)


def _pick_screen(home: str, claim: claims.Claim, channels: list[dict]) -> Screen:
    issued = channel_label(
        discovery.get_channel(claim.side, claim.channel_id),
        claim.channel_id,
    )
    here = PLATFORM_NAME[home]
    buttons = [
        Button(
            _button_text(channel_label(entry, int(entry["id"]))),
            payload=f"lnk:p:{entry['id']}",
        )
        for entry in channels
    ]
    buttons.append(_menu_button())
    return Screen(
        f"Код подошёл для «{issued}».\nВыберите канал {here}, который с ним связать.",
        buttons,
    )


async def _confirm_screen(home: str, user_id: int) -> Screen:
    pending = claims.get_pending(home, user_id)
    if pending is None or pending.chosen_id is None:
        return _missing_pending()
    tg_id, max_id = _pair_ids(pending)
    tg_label = channel_label(discovery.get_channel("tg", tg_id), tg_id)
    max_label = channel_label(discovery.get_channel("max", max_id), max_id)
    return Screen(
        f"Связать «{tg_label}» в Telegram и «{max_label}» в MAX?",
        [
            Button("Да, связать", payload="lnk:y"),
            Button("Нет", payload="lnk:n"),
        ],
    )


def _pair_ids(pending: claims.Pending) -> tuple[int, int]:
    if pending.claim.side == "tg":
        return pending.claim.channel_id, int(pending.chosen_id or 0)
    return int(pending.chosen_id or 0), pending.claim.channel_id


async def pick(home: str, user_id: int, channel_id: int | None) -> Screen:
    pending = claims.get_pending(home, user_id)
    if pending is None or channel_id is None:
        return _missing_pending()
    if not await user_can_manage(home, user_id, channel_id):
        return _with_menu(Screen("Этот канал вам недоступен."))
    paired_ids = config.TG_CHANNEL_IDS if home == "tg" else config.MAX_CHANNEL_IDS
    if channel_id in paired_ids:
        return _with_menu(Screen("Этот канал уже связан. Выберите другой."))
    if not claims.set_chosen(home, user_id, channel_id):
        return _missing_pending()
    return await _confirm_screen(home, user_id)


async def confirm(home: str, user_id: int) -> Screen:
    pending = claims.get_pending(home, user_id)
    if pending is None or pending.chosen_id is None:
        return _missing_pending()
    claim = pending.claim
    chosen_id = pending.chosen_id
    if not await user_can_manage(home, user_id, chosen_id):
        return _with_menu(Screen("Этот канал вам недоступен."))
    if not await user_can_manage(claim.side, claim.issuer_user_id, claim.channel_id):
        claims.consume_pending(home, user_id)
        return _with_menu(
            Screen("Права на исходный канал уже не подтверждаются. Запросите новый код.")
        )
    linked_by_tg = claim.issuer_user_id if claim.side == "tg" else user_id
    linked_by_max = user_id if claim.side == "tg" else claim.issuer_user_id
    tg_id, max_id = _pair_ids(pending)
    try:
        pairs.add_pair(
            tg_id,
            max_id,
            linked_by_tg=linked_by_tg,
            linked_by_max=linked_by_max,
        )
    except pairs.PairError as exc:
        return _with_menu(Screen(str(exc)))
    claims.consume_pending(home, user_id)
    tg_label = channel_label(discovery.get_channel("tg", tg_id), tg_id)
    max_label = channel_label(discovery.get_channel("max", max_id), max_id)
    text = f"Готово. «{tg_label}» и «{max_label}» синхронизируются в обе стороны."
    await _notify(claim.side, claim.issuer_user_id, text)
    return _with_menu(Screen(text))


async def cancel(home: str, user_id: int) -> Screen:
    pending = claims.get_pending(home, user_id)
    claims.release(home, user_id)
    if pending is None:
        return await menu(home, user_id)
    return _with_menu(
        Screen("Отменено. Код ещё можно отправить, пока не истекли 10 минут.")
    )


async def unlink_menu(home: str, user_id: int) -> Screen:
    buttons: list[Button] = []
    for pair in pairs.list_pairs():
        home_id = pair["tg_id"] if home == "tg" else pair["max_id"]
        if not await user_can_manage(home, user_id, home_id):
            continue
        label = channel_label(discovery.get_channel(home, home_id), home_id)
        buttons.append(Button(_button_text(f"Снять: {label}"), payload=f"lnk:u:{pair['tg_id']}"))
    if not buttons:
        return _with_menu(Screen("Нет связей, которые вы можете снять."))
    buttons.append(_menu_button())
    return Screen("Выберите связь:", buttons)


async def unlink_ask(home: str, user_id: int, tg_id: int | None) -> Screen:
    if tg_id is None:
        return await unlink_menu(home, user_id)
    pair = pairs.pair_by_tg(tg_id)
    if pair is None:
        return _with_menu(Screen("Такой связи уже нет."))
    home_id = pair["tg_id"] if home == "tg" else pair["max_id"]
    if not await user_can_manage(home, user_id, home_id):
        return _with_menu(Screen("Эту связь вы снять не можете."))
    tg_label = channel_label(discovery.get_channel("tg", pair["tg_id"]), pair["tg_id"])
    max_label = channel_label(discovery.get_channel("max", pair["max_id"]), pair["max_id"])
    return Screen(
        f"Снять связь «{tg_label}» ↔ «{max_label}»?\nНовые посты пересылаться не будут.",
        [
            Button("Да, снять", payload=f"lnk:uy:{tg_id}"),
            Button("Нет", payload="lnk:home"),
        ],
    )


async def unlink_do(home: str, user_id: int, tg_id: int | None) -> Screen:
    if tg_id is None:
        return await unlink_menu(home, user_id)
    pair = pairs.pair_by_tg(tg_id)
    if pair is None:
        return _with_menu(Screen("Такой связи уже нет."))
    home_id = pair["tg_id"] if home == "tg" else pair["max_id"]
    if not await user_can_manage(home, user_id, home_id):
        return _with_menu(Screen("Эту связь вы снять не можете."))
    removed = pairs.remove_by_tg(tg_id)
    text = "Связь снята. Новые посты больше не пересылаются."
    if removed:
        other = OTHER[home]
        other_user = removed.get("linked_by_max") if other == "max" else removed.get("linked_by_tg")
        if other_user:
            await _notify(other, int(other_user), text)
    return _with_menu(Screen(text))


async def handle_payload(home: str, user_id: int, payload: str) -> Screen:
    action, arg = _split_payload(payload)
    channel_id = _parse_id(arg)
    if action == "home":
        return await menu(home, user_id)
    if action == "i" and channel_id is not None:
        return await issue(home, user_id, channel_id)
    if action == "p":
        return await pick(home, user_id, channel_id)
    if action == "y":
        return await confirm(home, user_id)
    if action == "n":
        return await cancel(home, user_id)
    if action == "u":
        return await unlink_ask(home, user_id, channel_id)
    if action == "uy":
        return await unlink_do(home, user_id, channel_id)
    return await menu(home, user_id)


async def _notify(platform: str, user_id: int, text: str) -> None:
    try:
        if platform == "tg":
            await _tg_bot().send_message(user_id, text)
        else:
            await _max_bot().send_message(user_id=user_id, text=text)
    except Exception as exc:
        logger.info("Could not notify %s user %s: %s", platform, user_id, exc)

"""Persist auto-discovered Telegram / MAX channels."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STORE_PATH = DATA_DIR / "discovered_channels.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_store() -> dict[str, Any]:
    return {"telegram": {}, "max": {}}


def load_store() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return _empty_store()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", STORE_PATH, exc)
        return _empty_store()
    data.setdefault("telegram", {})
    data.setdefault("max", {})
    return data


def save_store(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STORE_PATH)


def _bucket(platform: str) -> str:
    if platform in {"tg", "telegram"}:
        return "telegram"
    if platform == "max":
        return "max"
    raise ValueError(f"Unknown platform: {platform}")


def register_telegram_channel(
    chat_id: int,
    title: str | None = None,
    username: str | None = None,
    status: str = "administrator",
    added_by: int | None = None,
    added_by_name: str | None = None,
) -> dict[str, Any]:
    store = load_store()
    key = str(chat_id)
    prev = store.get("telegram", {}).get(key, {})
    entry = {
        "id": chat_id,
        "title": title or prev.get("title"),
        "username": username if username is not None else prev.get("username"),
        "status": status,
        "added_by": added_by if added_by is not None else prev.get("added_by"),
        "added_by_name": added_by_name or prev.get("added_by_name"),
        "updated_at": _now(),
        "paired": chat_id in config.TG_CHANNEL_IDS,
    }
    store["telegram"][key] = entry
    save_store(store)
    return entry


def register_max_channel(
    chat_id: int,
    title: str | None = None,
    status: str = "added",
    added_by: int | None = None,
    added_by_name: str | None = None,
) -> dict[str, Any]:
    store = load_store()
    key = str(chat_id)
    prev = store.get("max", {}).get(key, {})
    entry = {
        "id": chat_id,
        "title": title or prev.get("title"),
        "status": status,
        "added_by": added_by if added_by is not None else prev.get("added_by"),
        "added_by_name": added_by_name or prev.get("added_by_name"),
        "updated_at": _now(),
        "paired": chat_id in config.MAX_CHANNEL_IDS,
    }
    store["max"][key] = entry
    save_store(store)
    return entry


def is_linkable(entry: dict[str, Any] | None, platform: str) -> bool:
    if not entry:
        return False
    if platform in {"tg", "telegram"}:
        return entry.get("status") in {"administrator", "creator"}
    return entry.get("status") == "channel"


def get_channel(platform: str, chat_id: int) -> dict[str, Any] | None:
    store = load_store()
    entry = store[_bucket(platform)].get(str(chat_id))
    return dict(entry) if entry else None


def list_linkable(platform: str) -> list[dict[str, Any]]:
    store = load_store()
    key = "tg" if platform in {"tg", "telegram"} else "max"
    return [dict(entry) for entry in store[_bucket(platform)].values() if is_linkable(entry, key)]


def set_paired(platform: str, chat_id: int, paired: bool) -> None:
    store = load_store()
    entry = store[_bucket(platform)].get(str(chat_id))
    if not entry or entry.get("paired") == paired:
        return
    entry["paired"] = paired
    entry["updated_at"] = _now()
    save_store(store)


def sync_paired_flags() -> None:
    store = load_store()
    changed = False
    for bucket, ids in (
        ("telegram", config.TG_CHANNEL_IDS),
        ("max", config.MAX_CHANNEL_IDS),
    ):
        for entry in store.get(bucket, {}).values():
            paired = entry.get("id") in ids
            if entry.get("paired") != paired:
                entry["paired"] = paired
                entry["updated_at"] = _now()
                changed = True
    if changed:
        save_store(store)


def mark_telegram_removed(chat_id: int) -> None:
    store = load_store()
    key = str(chat_id)
    entry = store["telegram"].get(key)
    if not entry:
        return
    entry["status"] = "removed"
    entry["updated_at"] = _now()
    entry["paired"] = chat_id in config.TG_CHANNEL_IDS
    save_store(store)


def mark_max_removed(chat_id: int) -> None:
    store = load_store()
    key = str(chat_id)
    entry = store["max"].get(key)
    if not entry:
        return
    entry["status"] = "removed"
    entry["updated_at"] = _now()
    entry["paired"] = chat_id in config.MAX_CHANNEL_IDS
    save_store(store)


def log_discovery_summary() -> None:
    store = load_store()
    tg_active = [
        e
        for e in store["telegram"].values()
        if e.get("status") not in {"removed", "left", "kicked"}
    ]
    max_active = [
        e
        for e in store["max"].values()
        if e.get("status") not in {"removed", "left"}
    ]

    unpaired_tg = [e for e in tg_active if e["id"] not in config.TG_CHANNEL_IDS]
    unpaired_max = [e for e in max_active if e["id"] not in config.MAX_CHANNEL_IDS]

    if not unpaired_tg and not unpaired_max:
        logger.info(
            "Channel discovery: no unpaired channels "
            "(store=%s, tg=%d, max=%d)",
            STORE_PATH,
            len(tg_active),
            len(max_active),
        )
        return

    logger.info("Unpaired discovered channels (link them in a private chat with the bot):")
    for entry in unpaired_tg:
        title = entry.get("title") or "?"
        username = entry.get("username")
        uname = f" @{username}" if username else ""
        logger.info(
            "  TG: %s%s id=%s added_by=%s",
            title,
            uname,
            entry["id"],
            entry.get("added_by"),
        )
    for entry in unpaired_max:
        title = entry.get("title") or "?"
        logger.info(
            "  MAX: %s id=%s added_by=%s",
            title,
            entry["id"],
            entry.get("added_by"),
        )

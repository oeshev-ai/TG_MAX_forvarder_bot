"""Persistent mapping between Telegram and MAX message ids."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STORE_PATH = DATA_DIR / "message_map.json"
_lock = threading.Lock()


def _empty() -> dict[str, Any]:
    return {"tg_to_max": {}, "max_to_tg": {}}


def _load() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return _empty()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read message map: %s", exc)
        return _empty()
    data.setdefault("tg_to_max", {})
    data.setdefault("max_to_tg", {})
    return data


def _save(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STORE_PATH)


def _tg_key(tg_chat_id: int, message_id: int) -> str:
    return f"{tg_chat_id}:{message_id}"


def _max_key(max_chat_id: int, mid: str) -> str:
    return f"{max_chat_id}:{mid}"


def link_pair(
    *,
    tg_chat_id: int,
    tg_message_ids: list[int],
    max_chat_id: int,
    max_mid: str,
) -> None:
    """Record bidirectional mapping for a forwarded post."""
    if not tg_message_ids or not max_mid:
        return
    with _lock:
        data = _load()
        max_key = _max_key(max_chat_id, max_mid)
        # Drop previous reverse links for these tg ids
        for mid_old, entry in list(data["max_to_tg"].items()):
            ids = set(entry.get("tg_message_ids") or [])
            if ids & set(tg_message_ids) and entry.get("tg_chat_id") == tg_chat_id:
                for tg_id in list(ids):
                    data["tg_to_max"].pop(_tg_key(tg_chat_id, tg_id), None)
                data["max_to_tg"].pop(mid_old, None)

        data["max_to_tg"][max_key] = {
            "tg_chat_id": tg_chat_id,
            "tg_message_ids": list(tg_message_ids),
            "max_chat_id": max_chat_id,
            "max_mid": max_mid,
        }
        for message_id in tg_message_ids:
            data["tg_to_max"][_tg_key(tg_chat_id, message_id)] = {
                "max_chat_id": max_chat_id,
                "max_mid": max_mid,
            }
        _save(data)


def get_max_for_tg(tg_chat_id: int, message_id: int) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        return data["tg_to_max"].get(_tg_key(tg_chat_id, message_id))


def get_tg_for_max(max_chat_id: int, mid: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        return data["max_to_tg"].get(_max_key(max_chat_id, mid))


def unlink_by_tg(tg_chat_id: int, message_id: int) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        tg_key = _tg_key(tg_chat_id, message_id)
        entry = data["tg_to_max"].pop(tg_key, None)
        if not entry:
            _save(data)
            return None
        max_key = _max_key(entry["max_chat_id"], entry["max_mid"])
        rev = data["max_to_tg"].get(max_key)
        if rev:
            ids = [i for i in rev.get("tg_message_ids", []) if i != message_id]
            if ids:
                rev["tg_message_ids"] = ids
            else:
                data["max_to_tg"].pop(max_key, None)
        _save(data)
        return entry


def unlink_by_max(max_chat_id: int, mid: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        max_key = _max_key(max_chat_id, mid)
        rev = data["max_to_tg"].pop(max_key, None)
        if not rev:
            _save(data)
            return None
        tg_chat_id = rev["tg_chat_id"]
        for message_id in rev.get("tg_message_ids") or []:
            data["tg_to_max"].pop(_tg_key(tg_chat_id, message_id), None)
        _save(data)
        return rev

"""Live Telegram ↔ MAX channel pairs.

The maps in ``config`` are updated in place so posting filters see new
links without a restart. The file ``data/channel_pairs.json`` is the
source of truth; ``CHANNEL_PAIRS`` in the environment is only a seed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import discovery

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STORE_PATH = DATA_DIR / "channel_pairs.json"

_pairs: list[dict[str, Any]] = []


class PairError(Exception):
    """Raised when a link would break the one-to-one rule."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_pairs() -> list[dict[str, Any]]:
    return [dict(item) for item in _pairs]


def pair_by_tg(tg_id: int) -> dict[str, Any] | None:
    for item in _pairs:
        if item["tg_id"] == tg_id:
            return dict(item)
    return None


def load() -> None:
    """Read the file, merge env seeds, and fill the live maps."""
    global _pairs
    loaded = _read()
    cleaned: list[dict[str, Any]] = []
    seen_tg: set[int] = set()
    seen_max: set[int] = set()
    for item in loaded:
        normalized = _normalize(item)
        if normalized is None:
            continue
        tg_id = normalized["tg_id"]
        max_id = normalized["max_id"]
        if tg_id in seen_tg or max_id in seen_max:
            logger.warning("Skip duplicate saved pair TG %s MAX %s", tg_id, max_id)
            continue
        seen_tg.add(tg_id)
        seen_max.add(max_id)
        cleaned.append(normalized)

    imported = 0
    for seed in config.ENV_CHANNEL_PAIRS:
        if seed.tg_id in seen_tg:
            current = next(item for item in cleaned if item["tg_id"] == seed.tg_id)
            if current["max_id"] != seed.max_id:
                logger.warning(
                    "CHANNEL_PAIRS %s:%s conflicts with a saved pair, keeping the saved one",
                    seed.tg_id,
                    seed.max_id,
                )
            continue
        if seed.max_id in seen_max:
            logger.warning(
                "CHANNEL_PAIRS MAX %s is already linked, skip TG %s",
                seed.max_id,
                seed.tg_id,
            )
            continue
        cleaned.append(
            {
                "tg_id": seed.tg_id,
                "max_id": seed.max_id,
                "linked_by_tg": None,
                "linked_by_max": None,
                "linked_at": _now(),
                "source": "env",
            }
        )
        seen_tg.add(seed.tg_id)
        seen_max.add(seed.max_id)
        imported += 1

    _pairs = cleaned
    _reindex()
    _write(_pairs)
    if imported:
        logger.info("Imported %d pair(s) from CHANNEL_PAIRS", imported)
    _ensure_discovery_stubs(_pairs)
    discovery.sync_paired_flags()
    logger.info("Channel pairs loaded: %d (%s)", len(_pairs), STORE_PATH)


def _ensure_discovery_stubs(items: list[dict[str, Any]]) -> None:
    """Create discovery rows for pairs that never went through auto-discover.

    Env-imported pairs otherwise exist only in channel_pairs.json, so the
    private-chat wizard cannot show titles or let an admin unlink them.
    """
    for item in items:
        tg_id = item["tg_id"]
        max_id = item["max_id"]
        if discovery.get_channel("tg", tg_id) is None:
            discovery.register_telegram_channel(
                tg_id,
                title=f"TG {tg_id}",
                status="administrator",
            )
        if discovery.get_channel("max", max_id) is None:
            discovery.register_max_channel(
                max_id,
                title=f"MAX {max_id}",
                status="channel",
            )


def add_pair(
    tg_id: int,
    max_id: int,
    *,
    linked_by_tg: int | None = None,
    linked_by_max: int | None = None,
    source: str = "code",
) -> dict[str, Any]:
    if tg_id in config.TG_TO_MAX:
        raise PairError("Этот Telegram-канал уже связан.")
    if max_id in config.MAX_TO_TG:
        raise PairError("Этот MAX-канал уже связан.")
    record = {
        "tg_id": tg_id,
        "max_id": max_id,
        "linked_by_tg": linked_by_tg,
        "linked_by_max": linked_by_max,
        "linked_at": _now(),
        "source": source,
    }
    updated = [*_pairs, record]
    _write(updated)
    _pairs.append(record)
    config.TG_TO_MAX[tg_id] = max_id
    config.MAX_TO_TG[max_id] = tg_id
    config.TG_CHANNEL_IDS.add(tg_id)
    config.MAX_CHANNEL_IDS.add(max_id)
    discovery.set_paired("tg", tg_id, True)
    discovery.set_paired("max", max_id, True)
    logger.info("Linked TG %s ↔ MAX %s", tg_id, max_id)
    return dict(record)


def remove_by_tg(tg_id: int) -> dict[str, Any] | None:
    max_id = config.TG_TO_MAX.get(tg_id)
    if max_id is None:
        return None
    removed = next(item for item in _pairs if item["tg_id"] == tg_id)
    updated = [item for item in _pairs if item["tg_id"] != tg_id]
    _write(updated)
    _pairs[:] = updated
    config.TG_TO_MAX.pop(tg_id, None)
    config.MAX_TO_TG.pop(max_id, None)
    config.TG_CHANNEL_IDS.discard(tg_id)
    config.MAX_CHANNEL_IDS.discard(max_id)
    discovery.set_paired("tg", tg_id, False)
    discovery.set_paired("max", max_id, False)
    logger.info("Unlinked TG %s ↔ MAX %s", tg_id, max_id)
    return dict(removed)


def _normalize(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        logger.warning("Skip invalid pair entry: %r", item)
        return None
    try:
        tg_id = int(item["tg_id"])
        max_id = int(item["max_id"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Skip invalid pair entry: %r", item)
        return None
    return {
        "tg_id": tg_id,
        "max_id": max_id,
        "linked_by_tg": _optional_int(item.get("linked_by_tg")),
        "linked_by_max": _optional_int(item.get("linked_by_max")),
        "linked_at": item.get("linked_at") or _now(),
        "source": item.get("source") or "file",
    }


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reindex() -> None:
    config.TG_TO_MAX.clear()
    config.MAX_TO_TG.clear()
    config.TG_CHANNEL_IDS.clear()
    config.MAX_CHANNEL_IDS.clear()
    for item in _pairs:
        config.TG_TO_MAX[item["tg_id"]] = item["max_id"]
        config.MAX_TO_TG[item["max_id"]] = item["tg_id"]
        config.TG_CHANNEL_IDS.add(item["tg_id"])
        config.MAX_CHANNEL_IDS.add(item["max_id"])


def _read() -> list[Any]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", STORE_PATH, exc)
        return []
    raw = data.get("pairs", []) if isinstance(data, dict) else []
    return raw if isinstance(raw, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"pairs": items}
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STORE_PATH)

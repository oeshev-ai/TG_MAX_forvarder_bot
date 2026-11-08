import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require_str(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        print(f"Missing required env var: {name}", file=sys.stderr)
        raise SystemExit(1)
    return value.strip()


def _parse_int(raw: str, label: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        print(f"{label} must be an integer, got: {raw!r}", file=sys.stderr)
        raise SystemExit(1) from None


@dataclass(frozen=True)
class ChannelPair:
    tg_id: int
    max_id: int


def _parse_pairs() -> list[ChannelPair]:
    """
    CHANNEL_PAIRS=tg_id:max_id,tg_id:max_id,...

    Legacy fallback: TG_CHANNEL_ID + MAX_CHANNEL_ID (one pair).
    """
    raw = (os.getenv("CHANNEL_PAIRS") or "").strip()
    pairs: list[ChannelPair] = []

    if raw:
        for index, chunk in enumerate(raw.split(","), start=1):
            part = chunk.strip()
            if not part:
                continue
            if ":" not in part:
                print(
                    f"CHANNEL_PAIRS item #{index} must be tg_id:max_id, got: {part!r}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            tg_raw, max_raw = part.split(":", 1)
            pairs.append(
                ChannelPair(
                    tg_id=_parse_int(tg_raw, f"CHANNEL_PAIRS[{index}].tg_id"),
                    max_id=_parse_int(max_raw, f"CHANNEL_PAIRS[{index}].max_id"),
                )
            )
    else:
        tg_raw = (os.getenv("TG_CHANNEL_ID") or "").strip()
        max_raw = (os.getenv("MAX_CHANNEL_ID") or "").strip()
        if tg_raw and max_raw:
            pairs.append(
                ChannelPair(
                    tg_id=_parse_int(tg_raw, "TG_CHANNEL_ID"),
                    max_id=_parse_int(max_raw, "MAX_CHANNEL_ID"),
                )
            )

    tg_ids = [p.tg_id for p in pairs]
    max_ids = [p.max_id for p in pairs]
    if len(tg_ids) != len(set(tg_ids)):
        print("Duplicate Telegram channel id in CHANNEL_PAIRS", file=sys.stderr)
        raise SystemExit(1)
    if len(max_ids) != len(set(max_ids)):
        print("Duplicate MAX channel id in CHANNEL_PAIRS", file=sys.stderr)
        raise SystemExit(1)

    return pairs


MAX_BOT_TOKEN = _require_str("MAX_BOT_TOKEN")
TELEGRAM_BOT_TOKEN = _require_str("TELEGRAM_BOT_TOKEN")

# Optional seed from the environment. Live pairs are loaded into the maps below.
ENV_CHANNEL_PAIRS = _parse_pairs()

# pairs.load() / add_pair() / remove_by_tg() mutate these objects in place.
# Message filters keep a reference to the sets, so do not rebind the names.
TG_TO_MAX: dict[int, int] = {}
MAX_TO_TG: dict[int, int] = {}
TG_CHANNEL_IDS: set[int] = set()
MAX_CHANNEL_IDS: set[int] = set()

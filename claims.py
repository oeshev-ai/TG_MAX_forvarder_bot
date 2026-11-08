"""One-time codes that prove the same person linked both channels.

Codes live in memory. A restart drops them; the person asks for a new one.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 8
TTL_SEC = 10 * 60
MAX_FAILURES = 5
FAILURE_WINDOW_SEC = 10 * 60

_SEPARATORS = re.compile(r"[\s\-–—]+")


@dataclass
class Claim:
    code: str
    side: str
    channel_id: int
    issuer_user_id: int
    expires_at: float


@dataclass
class Pending:
    claim: Claim
    home: str
    user_id: int
    chosen_id: int | None


_claims: dict[str, Claim] = {}
_pending: dict[tuple[str, int], Pending] = {}
_failures: dict[tuple[str, int], list[float]] = {}


def reset() -> None:
    _claims.clear()
    _pending.clear()
    _failures.clear()


def normalize(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch in ALPHABET)


def looks_like_code(raw: str) -> bool:
    text = (raw or "").strip()
    if not text or text.startswith("/"):
        return False
    compact = normalize(text)
    if len(compact) != CODE_LENGTH:
        return False
    return _SEPARATORS.sub("", text).upper() == compact


def pretty(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def is_blocked(home: str, user_id: int, *, now: float | None = None) -> bool:
    return len(_prune_failures(home, user_id, now)) >= MAX_FAILURES


def note_failure(home: str, user_id: int, *, now: float | None = None) -> None:
    moment = time.time() if now is None else now
    recent = _prune_failures(home, user_id, moment)
    recent.append(moment)
    _failures[(home, user_id)] = recent


def issue(side: str, channel_id: int, issuer_user_id: int, *, now: float | None = None) -> str:
    moment = time.time() if now is None else now
    _drop_channel(side, channel_id)
    code = _unique_code()
    _claims[code] = Claim(
        code=code,
        side=side,
        channel_id=channel_id,
        issuer_user_id=issuer_user_id,
        expires_at=moment + TTL_SEC,
    )
    return code


def lookup(raw: str, *, now: float | None = None) -> Claim | None:
    code = normalize(raw)
    claim = _claims.get(code)
    if claim is None:
        return None
    if not _alive(claim, now):
        _claims.pop(code, None)
        return None
    return claim


def hold(
    raw: str,
    home: str,
    user_id: int,
    chosen_id: int | None,
    *,
    now: float | None = None,
) -> Claim | None:
    claim = lookup(raw, now=now)
    if claim is None:
        return None
    release(home, user_id, now=now)
    _claims.pop(claim.code, None)
    _pending[(home, user_id)] = Pending(
        claim=claim,
        home=home,
        user_id=user_id,
        chosen_id=chosen_id,
    )
    return claim


def get_pending(home: str, user_id: int, *, now: float | None = None) -> Pending | None:
    pending = _pending.get((home, user_id))
    if pending is None:
        return None
    if not _alive(pending.claim, now):
        _pending.pop((home, user_id), None)
        return None
    return pending


def set_chosen(home: str, user_id: int, channel_id: int, *, now: float | None = None) -> bool:
    pending = get_pending(home, user_id, now=now)
    if pending is None:
        return False
    pending.chosen_id = channel_id
    return True


def release(home: str, user_id: int, *, now: float | None = None) -> None:
    pending = _pending.pop((home, user_id), None)
    if pending is None:
        return
    if _alive(pending.claim, now) and pending.claim.code not in _claims:
        _claims[pending.claim.code] = pending.claim


def consume_pending(home: str, user_id: int, *, now: float | None = None) -> Pending | None:
    pending = get_pending(home, user_id, now=now)
    if pending is None:
        return None
    _pending.pop((home, user_id), None)
    return pending


def _alive(claim: Claim, now: float | None) -> bool:
    moment = time.time() if now is None else now
    return claim.expires_at > moment


def _unique_code() -> str:
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if code not in _claims:
            return code
    raise RuntimeError("Could not allocate a link code")


def _drop_channel(side: str, channel_id: int) -> None:
    for code, claim in list(_claims.items()):
        if claim.side == side and claim.channel_id == channel_id:
            del _claims[code]
    for key, pending in list(_pending.items()):
        if pending.claim.side == side and pending.claim.channel_id == channel_id:
            del _pending[key]


def _prune_failures(home: str, user_id: int, now: float | None) -> list[float]:
    moment = time.time() if now is None else now
    key = (home, user_id)
    recent = [stamp for stamp in _failures.get(key, []) if moment - stamp < FAILURE_WINDOW_SEC]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent

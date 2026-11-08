"""Guard against echo loops in bidirectional TG ↔ MAX bridging."""

from __future__ import annotations

import hashlib
import time


class IdGuard:
    """TTL set of recently sent message ids / fingerprints."""

    def __init__(self, ttl_sec: float = 180.0) -> None:
        self._ttl = ttl_sec
        self._items: dict[str, float] = {}

    def mark(self, key: str) -> None:
        now = time.monotonic()
        self._purge(now)
        self._items[key] = now + self._ttl

    def seen(self, key: str) -> bool:
        now = time.monotonic()
        self._purge(now)
        return key in self._items

    def consume(self, key: str) -> bool:
        """Return True once if key exists, then remove it (one-shot echo guard)."""
        now = time.monotonic()
        self._purge(now)
        if key not in self._items:
            return False
        del self._items[key]
        return True

    def _purge(self, now: float) -> None:
        expired = [key for key, expires_at in self._items.items() if expires_at <= now]
        for key in expired:
            del self._items[key]


def content_fingerprint(text: str, attachments=None) -> str:
    """
    Stable fingerprint of MAX message content.

    Includes attachment identity (photo_id/token/url), not just attachment count,
    so two different images with empty captions do not collide.
    """
    parts = [text or ""]
    for att in attachments or []:
        payload = getattr(att, "payload", None)
        att_type = str(getattr(att, "type", "") or "")
        photo_id = str(getattr(payload, "photo_id", "") or "")
        token = str(getattr(payload, "token", "") or "")
        url = str(getattr(payload, "url", "") or "")
        # Video token often lives on the attachment itself.
        vtoken = str(getattr(att, "token", "") or "")
        filename = str(getattr(att, "filename", "") or "")
        parts.append(f"{att_type}:{photo_id}:{token}:{vtoken}:{url}:{filename}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# Outbound ids we created — skip when the same update comes back.
# Fingerprints use a shorter TTL; mids can live longer via the same guard.
max_outbound = IdGuard(ttl_sec=300.0)
tg_outbound = IdGuard(ttl_sec=300.0)
fp_outbound = IdGuard(ttl_sec=60.0)

"""Shared helpers for normalizing RFID tag and tray identifiers."""


def normalize_hex(value: str | None) -> str:
    if not value:
        return ""
    hex_chars = "".join(ch for ch in str(value).strip() if ch in "0123456789abcdefABCDEF")
    return hex_chars.upper()


def normalize_tag_uid(value: str | None) -> str:
    uid = normalize_hex(value)
    # DB column is VARCHAR(16), so keep the least-significant bytes if longer.
    if len(uid) > 16:
        uid = uid[-16:]
    return uid


def normalize_tray_uuid(value: str | None) -> str:
    uuid = normalize_hex(value)
    # DB column is VARCHAR(32). Keep canonical 32-char UUID when possible.
    if len(uuid) >= 32:
        uuid = uuid[:32]
    return uuid


def tag_matches_row(scanned: str | None, tag_uid: str | None, sibling_tag_uid: str | None) -> bool:
    """THE tag comparison: does ``scanned`` name the roll this row stands for?

    A Bambu roll carries **two** RFID chips — one per flange side — sharing a single
    ``tray_uuid`` (the sibling-tag identity law, live-proven 4/4 fleet slots on
    2026-08-01). The AMS reads whichever side faces its antenna, so a row's tag identity
    is a PAIR, not one value, and ``scanned != row.tag_uid`` is NOT evidence of a
    different roll. This is the one place that knows that, so every "same roll?" tag
    question resolves identically wherever it is asked.

    ``sibling_tag_uid`` is the second chip once it has been sighted (persisted by the
    slot pipeline's sibling-read path); NULL means "only one side has ever been read",
    which is the normal state for a roll that has never been re-seated the other way up.

    Comparison is case-insensitive and whitespace-tolerant but deliberately does NOT
    run :func:`normalize_hex`: callers that need hex hygiene (the tag matcher's variance
    lane) normalize before calling, and stripping non-hex characters here would collapse
    distinct non-hex identifiers onto each other. An empty ``scanned`` never matches —
    "no tag was read" is not an identification, and an empty stored side is not a wildcard.
    """
    probe = (scanned or "").strip().upper()
    if not probe:
        return False
    return probe in {(tag_uid or "").strip().upper(), (sibling_tag_uid or "").strip().upper()}

"""Print-file filename validation matching Bambu Studio's save-dialog rules.

The Bambu printer SD card is FAT32/exFAT. Names containing the Windows /
DOS-reserved set (``< > : " / \\ | ? *``), ASCII control characters
(0x00-0x1F), or trailing dots / spaces cannot be created on it — FTP fails
with ``553 Could not create file`` (#1540). Bambu Studio refuses to save
such names client-side; Bambuddy now does the same at the rename, upload,
and dispatch boundaries so the failure surfaces with a clear message
instead of an obscure FTP error after the user has already hit Print.
"""

INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# FAT/exFAT cap on a single path component; UTF-8 byte length, not codepoints,
# because that is what the on-disk encoding limit actually is.
MAX_FILENAME_BYTES = 255


class InvalidFilenameError(ValueError):
    """Filename contains characters or shape the printer SD card rejects.

    ``char`` is the first offending character when the failure is a
    character-set violation, or ``None`` for structural failures (empty,
    bare ``.``, trailing space, too long, etc.). The frontend echoes it
    back to the user in the Bambu Studio-style error message.
    """

    def __init__(self, message: str, char: str | None = None):
        super().__init__(message)
        self.char = char


def validate_print_filename(name: str) -> None:
    """Raise ``InvalidFilenameError`` if ``name`` would fail on the SD card.

    Matches Bambu Studio's save-dialog rejection set. Callers are expected
    to translate the exception into an HTTP 400 (or a clean dispatch
    rejection); the message is intentionally short and ASCII so it fits
    a translation template.
    """
    if not name or not name.strip():
        raise InvalidFilenameError("Filename cannot be empty")

    if name in (".", ".."):
        raise InvalidFilenameError("Filename cannot be '.' or '..'")

    for ch in name:
        if ch in INVALID_FILENAME_CHARS:
            raise InvalidFilenameError(f"Filename contains invalid character: {ch}", char=ch)
        if ord(ch) < 0x20:
            raise InvalidFilenameError("Filename contains a control character", char=ch)

    if name.endswith(" ") or name.endswith("."):
        raise InvalidFilenameError("Filename cannot end with a space or dot")

    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise InvalidFilenameError(f"Filename exceeds {MAX_FILENAME_BYTES} bytes")


def derive_remote_filename(filename: str) -> str:
    """Compute the SD-card filename used when uploading a sliced print file.

    Strips repeated trailing ``.gcode.3mf`` / ``.3mf`` suffixes until the
    bare stem remains, then appends a single ``.3mf``; spaces are
    replaced with underscores because the firmware parses
    ``ftp://{filename}`` as a URL.

    Canonical for both the dispatch uploader and the post-print SD
    cleanup — when the two drift apart the cleanup misses, and a
    library row whose stored filename ended up with a doubled
    ``.gcode.3mf`` (#1542) leaves the real file on the SD card. On A1
    firmware that lingering file becomes a ghost print on the next
    power-on (same family as the P1S behaviour in #374).

    Raises ``TypeError`` on non-string input rather than entering the
    strip loop, because a duck-typed object that returns truthy
    sentinels from ``endswith`` would never escape and the resulting
    unbounded allocation has cgroup-OOM'd the test runner under mocks.
    """
    if not isinstance(filename, str):
        raise TypeError(f"derive_remote_filename requires str, got {type(filename).__name__}")
    stem = filename
    while True:
        if stem.endswith(".gcode.3mf"):
            stem = stem[:-10]
        elif stem.endswith(".3mf"):
            stem = stem[:-4]
        else:
            break
    return f"{stem}.3mf".replace(" ", "_")


def print_identity_key(filename: str) -> str:
    """The one "is this the same print?" key.

    Basename → repeatedly strip a trailing ``.gcode.3mf`` / ``.3mf`` / ``.gcode``
    → remove a remaining ``.gcode`` token (the splicer names files
    ``<project>.gcode_L1-NN_spliced.3mf`` and the firmware drops that token from
    its echo) → fold spaces to underscores → lower-case.

    THE single normaliser for every "did this printer just finish the file we
    think it did?" comparison — terminal-status correlation
    (``farm_correlation._payload_names`` / ``_item_names``) and the foreign
    auto-eject identity check (``eject.manual._canonical_names``) both key on it.
    Before 2026-08-22 those two carried private variants, neither of which could
    see this farm's corpus: production names such as
    ``Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf`` come back
    from the printer as ``Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced``
    — the mid-stem ``.gcode`` token gone and spaces already underscored — so the
    foreign-plate rescue never fired once in five days of FOREIGN terminals.

    Deliberately NOT a fuzzy match: it removes ONE known token and folds ONE
    known separator. No edit distance, no prefix matching, no similarity score.
    Two names that differ by any real token still key differently, which is what
    keeps ``matched_by_name`` from crediting a foreign print to a farm unit.

    Distinct from :func:`derive_remote_filename`, which must keep producing the
    ACTUAL SD-card filename (post-print cleanup matches on it). This key is
    lossy on purpose and must never be used to name a file. Space folding
    mirrors that function exactly — no surrounding whitespace strip — because
    the echo the printer returns is derived from the uploaded name, so any strip
    here would desynchronise the two sides.

    Raises ``TypeError`` on non-string input for the same reason
    :func:`derive_remote_filename` does: a duck-typed object returning truthy
    sentinels from ``endswith`` never escapes the strip loop, and the resulting
    unbounded allocation has cgroup-OOM'd the test runner under mocks.
    """
    if not isinstance(filename, str):
        raise TypeError(f"print_identity_key requires str, got {type(filename).__name__}")
    # Lower-case up front so the suffix/token matching below is case-insensitive
    # (a ``.3MF`` upload keys the same as a ``.3mf`` one); the result is identical
    # to folding case last for every all-lowercase-suffix name.
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    while True:
        if stem.endswith(".gcode.3mf"):
            stem = stem[:-10]
        elif stem.endswith(".3mf"):
            stem = stem[:-4]
        elif stem.endswith(".gcode"):
            stem = stem[:-6]
        else:
            break
    return stem.replace(".gcode", "", 1).replace(" ", "_")

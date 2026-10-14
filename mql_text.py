"""One canonical text reader for MQL sources, MetaEditor/terminal logs and MQL-written files.

Shared by engine_core (classifier) and preview_bridge (renderer) so both see identical text.

Why this exists: the old readers trial-decoded with lenient codecs. bytes.decode('utf-16',
errors='ignore') never raises (pathlib's stream decoder happens to reject BOM-less input,
which is the only reason UTF-8 files survived), and a lenient utf-8 step ahead of cp1252
silently dropped legacy characters. Decoding is chosen from the bytes instead: BOM first,
then a NUL-pattern check for BOM-less UTF-16, then strict UTF-8, cp1252, latin1.

Supported inputs:
  * UTF-16LE / UTF-16BE with BOM  (MetaEditor's default for .mq5, MetaEditor/terminal logs,
    MQL FileOpen(FILE_TXT) without FILE_ANSI)
  * BOM-less UTF-16LE / BE        (detected from NUL byte positions)
  * UTF-8 with or without BOM
  * cp1252 / latin1               (legacy MT4 .mq4 files)
"""
from __future__ import annotations

from pathlib import Path

_SNIFF = 4096


def _bomless_utf16(data: bytes) -> str | None:
    """Return 'utf-16-le'/'utf-16-be' when the byte pattern is clearly UTF-16 without a BOM."""
    sample = data[:_SNIFF]
    if len(sample) < 4:
        return None
    if len(sample) % 2:
        sample = sample[:-1]
    half = len(sample) // 2
    even_nul = sample[0::2].count(0)
    odd_nul = sample[1::2].count(0)
    # Mostly-ASCII UTF-16 text has a NUL in every high byte. Require a clear majority on
    # one side and almost none on the other so ordinary binary/8-bit text never qualifies.
    if odd_nul >= half * 0.6 and even_nul <= half * 0.05:
        return 'utf-16-le'
    if even_nul >= half * 0.6 and odd_nul <= half * 0.05:
        return 'utf-16-be'
    return None


def decode_mql_bytes(data: bytes) -> str:
    """Decode raw bytes from an MQL source, log or MQL-written text file."""
    if not data:
        return ''
    if data[:2] == b'\xff\xfe':
        return data[2:].decode('utf-16-le', errors='replace')
    if data[:2] == b'\xfe\xff':
        return data[2:].decode('utf-16-be', errors='replace')
    if data[:3] == b'\xef\xbb\xbf':
        return data[3:].decode('utf-8', errors='replace')
    enc = _bomless_utf16(data)
    if enc:
        return data.decode(enc, errors='replace')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('cp1252')
    except UnicodeDecodeError:
        return data.decode('latin1')


def read_mql_text(path) -> str:
    """Read a file with decode_mql_bytes. Missing/unreadable files return ''."""
    try:
        return decode_mql_bytes(Path(path).read_bytes())
    except Exception:
        return ''


def read_log_text(path) -> str:
    """Read a MetaEditor compile log or terminal journal (UTF-16LE in practice).

    Same decoder as sources; kept as its own name so log call sites say what they read and
    can diverge later (e.g. tailing very large journals) without touching source decoding.
    """
    return read_mql_text(path)

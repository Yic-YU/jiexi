# backend/parsers/net_parser.py
from typing import Dict, Any

from . import ApplicationLayer


def _ascii_preview(data: bytes, max_len: int = 32) -> str:
    """Convert first max_len bytes to ASCII (non-printable chars replaced with '.')"""
    return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in data[:max_len])


def parse_generic_payload(data: bytes, transport: str) -> ApplicationLayer:
    """Fallback parser for non-MAVLink packets (returns basic payload info)"""
    length = len(data)
    preview_len = min(32, length)

    fields: Dict[str, Any] = {
        "summary": f"{transport} payload len={length}",
        "length": length,
        "preview_hex": data[:preview_len].hex(),
        "ascii_preview": _ascii_preview(data, preview_len),
    }

    return ApplicationLayer(
        protocol=transport.upper(),
        is_mavlink=False,
        msg_name=None,
        msg_id=None,
        fields=fields,
        raw_hex=data.hex(),
    )

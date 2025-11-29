from typing import Any, Dict, Tuple, Optional
import io
import struct

from pymavlink.dialects.v20 import common as mavlink2

from . import ApplicationLayer


def looks_like_mavlink(data: bytes) -> bool:
    """Check if bytes resemble a MAVLink frame (1 frame per UDP/TCP packet)"""
    if len(data) < 6:
        return False

    stx = data[0]
    if stx not in (0xFE, 0xFD):  # MAVLink v1/v2 start byte
        return False

    payload_len = data[1]
    min_len = 6 + payload_len + 2 if stx == 0xFE else 10 + payload_len + 2  # v1/v2 min length
    return len(data) >= min_len


def _parse_header(data: bytes) -> Tuple[Dict[str, Any], int, int]:
    """Parse MAVLink header (v1/v2)
    Returns: (header_dict, header_len, frame_len)
    frame_len: Full length of frame (including checksum and optional signature)
    """
    if len(data) < 6:
        raise ValueError("Data too short for MAVLink header")

    stx = data[0]
    payload_len = data[1]

    if stx == 0xFE:  # MAVLink v1
        version, header_len = 1, 6
        if len(data) < header_len + payload_len + 2:
            raise ValueError("Data too short for MAVLink v1 frame")
        
        seq, sysid, compid, msgid = data[2], data[3], data[4], data[5]
        incompat_flags = compat_flags = None
    elif stx == 0xFD:  # MAVLink v2
        version, header_len = 2, 10
        if len(data) < header_len + payload_len + 2:
            raise ValueError("Data too short for MAVLink v2 frame")
        
        incompat_flags, compat_flags = data[2], data[3]
        seq, sysid, compid = data[4], data[5], data[6]
        msgid = data[7] | (data[8] << 8) | (data[9] << 16)
    else:
        raise ValueError(f"Unknown MAVLink STX: {stx:#x}")

    checksum_end = header_len + payload_len + 2
    frame_len = checksum_end
    signature = None

    # Check for MAVLink v2 signature (13 bytes)
    if stx == 0xFD and len(data) >= checksum_end + 13:
        signature = data[checksum_end:checksum_end + 13].hex()
        frame_len += 13

    checksum = struct.unpack("<H", data[header_len + payload_len:checksum_end])[0]

    return {
        "version": version,
        "stx": f"0x{stx:02x}",
        "len": payload_len,
        "seq": seq,
        "sysid": sysid,
        "compid": compid,
        "msgid": msgid,
        "checksum": f"0x{checksum:04x}",
        "incompat_flags": incompat_flags,
        "compat_flags": compat_flags,
        "signature": signature,
    }, header_len, frame_len


def parse_mavlink_payload(data: bytes) -> ApplicationLayer:
    """Parse bytes as MAVLink frame (supports v1/v2)
    ApplicationLayer.fields structure:
    {
      "header": {...},  # Parsed header info
      "payload": {...}  # Message fields from msg.to_dict()
    }
    """
    header, header_len, frame_len = _parse_header(data)
    frame_bytes = data[:frame_len]
    payload_bytes = frame_bytes[header_len:header_len + header["len"]]

    # Parse frame with pymavlink
    mav = mavlink2.MAVLink(io.BytesIO())
    msg = None
    for b in frame_bytes:
        parsed = mav.parse_char(bytes([b]))
        if parsed:
            msg = parsed
            break

    msg_name: Optional[str] = None
    msg_id: Optional[int] = header["msgid"]
    payload_fields: Dict[str, Any]

    if not msg:
        payload_fields = {
            "_error": "Unable to parse MAVLink message payload",
            "_raw_payload_hex": payload_bytes.hex()
        }
    else:
        payload_fields = msg.to_dict()
        msg_name = msg.get_type()
        msg_id = msg.get_msgId()
        # Add extra metadata
        payload_fields.update({
            "_sysid": msg.get_srcSystem(),
            "_compid": msg.get_srcComponent(),
            "_msgid": msg_id,
            "_msg_name": msg_name
        })

    return ApplicationLayer(
        protocol="MAVLink",
        is_mavlink=True,
        msg_name=msg_name,
        msg_id=msg_id,
        fields={"header": header, "payload": payload_fields},
        raw_hex=frame_bytes.hex()  # Full frame hex (stx to signature)
    )

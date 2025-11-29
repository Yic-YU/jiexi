# backend/parsers/dispatcher.py
from typing import Optional

from . import LinkLayer, NetworkLayer, ApplicationLayer, PacketResult
from .mavlink_parser import looks_like_mavlink, parse_mavlink_payload
from .net_parser import parse_generic_payload


def dispatch_packet(
    data: bytes,
    *,
    src_ip: str,
    src_port: int,
    dst_ip: str,
    dst_port: int,
    transport: str = "UDP",
    link: Optional[LinkLayer] = None,
) -> PacketResult:
    """
    调度函数：根据 payload + IP/端口 + 可选 LinkLayer，生成 PacketResult。
    """
    transport_upper = transport.upper()

    network = NetworkLayer(
        protocol=transport_upper,
        src_ip=src_ip,
        src_port=src_port,
        dst_ip=dst_ip,
        dst_port=dst_port,
    )

    # 应用层：先判断是不是 MAVLink
    if looks_like_mavlink(data):
        app: ApplicationLayer = parse_mavlink_payload(data)
    else:
        app = parse_generic_payload(data, transport_upper)

    return PacketResult(
        link=link,
        network=network,
        application=app,
    )
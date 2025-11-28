# backend/parsers/net_parser.py
from scapy.all import Ether, IP, IPv6, TCP, UDP


def parse_packet(raw_bytes: bytes) -> dict:
    """
    使用 scapy 解析一条原始数据包：
    Ethernet -> IP/IPv6 -> TCP/UDP -> 应用层载荷
    返回一个结构化的 dict
    """
    pkt = Ether(raw_bytes)

    result: dict = {
        "layers": []
    }

    # 以太网层
    eth_info = {
        "layer": "Ethernet",
        "src": pkt.src,
        "dst": pkt.dst,
        "type": hex(pkt.type),
    }
    result["layers"].append(eth_info)

    # IP / IPv6 层
    l3 = pkt.payload
    if isinstance(l3, (IP, IPv6)):
        ip_info = {
            "layer": l3.__class__.__name__,
            "src": getattr(l3, "src", None),
            "dst": getattr(l3, "dst", None),
            "proto": getattr(l3, "proto", None),
        }
        result["layers"].append(ip_info)

        # 传输层
        l4 = l3.payload
        if isinstance(l4, (TCP, UDP)):
            l4_info = {
                "layer": l4.__class__.__name__,
                "sport": getattr(l4, "sport", None),
                "dport": getattr(l4, "dport", None),
            }
            result["layers"].append(l4_info)

            # 应用层载荷
            payload = bytes(l4.payload)
            result["payload_raw_hex"] = payload.hex()
            try:
                result["payload_as_text"] = payload.decode("utf-8")
            except UnicodeDecodeError:
                result["payload_as_text"] = None

    return result

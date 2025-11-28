# backend/parsers/dispatcher.py
from __future__ import annotations
from typing import Dict, Any, Tuple, Optional

# 你原来的“传统数据包解析”函数
from .net_parser import parse_packet as parse_traditional_packet
# MAVLink 解析
from .mavlink_parser import parse_mavlink_bytes


def parse_packet(data: bytes, addr: Optional[Tuple[str, int]] = None) -> Dict[str, Any]:
    """
    顶层调度函数：
      - data: 从 UDP 收到的原始字节
      - addr: (ip, port)，主函数传进来（可选）

    返回统一结构，前端只要看 packet_type 就知道用哪个 UI：
      packet_type: "mavlink" | "traditional" | "unknown"
    """
    result: Dict[str, Any] = {
        "length": len(data),
        "raw_hex": data.hex(),
        "packet_type": "unknown",   # 默认 unknown
    }

    # 把来源地址也带上，方便前端显示
    if addr is not None:
        ip, port = addr
        result["src_addr"] = f"{ip}:{port}"

    # ===== 1. 先尝试按 MAVLink 解析 =====
    mav = parse_mavlink_bytes(data)
    if mav.get("is_mavlink"):
        result["packet_type"] = "mavlink"
        result["mavlink"] = mav
        return result

    # ===== 2. 再尝试传统解析 =====
    try:
        trad = parse_traditional_packet(data)
        result["packet_type"] = "traditional"
        result["traditional"] = trad
    except Exception as e:
        # 传统解析失败，就保持 unknown，并写个错误
        result["packet_type"] = "unknown"
        result["error"] = f"traditional parse failed: {e}"

    return result

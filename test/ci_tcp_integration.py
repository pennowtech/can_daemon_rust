import json
import logging
import os
import socket
import subprocess
import time

logging.basicConfig(
    level=logging.INFO, format="PY %(asctime)s %(levelname)s %(message)s"
)

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "9500"))
IFACE = os.environ.get("BRIDGE_IFACE", "vcan0")


def send_jsonl(sock, obj):
    line = json.dumps(obj) + "\n"
    sock.sendall(line.encode("utf-8"))
    logging.info("-> %s", obj)


def recv_line(sock, timeout_s=3.0):
    sock.settimeout(timeout_s)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed")
        buf += chunk
    line, _rest = buf.split(b"\n", 1)
    return line.decode("utf-8")


def recv_jsonl(sock, timeout_s=3.0):
    obj = json.loads(recv_line(sock, timeout_s=timeout_s))
    logging.info("<- %s", obj)
    return obj


def wait_port(host, port, timeout_s=10.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.5)
            return s
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port not ready: {host}:{port}")


def main():
    logging.info("connecting to %s:%d", HOST, PORT)
    sock = wait_port(HOST, PORT, timeout_s=15.0)

    # hello + ack
    hello = recv_jsonl(sock, timeout_s=5.0)
    assert hello["type"] == "hello", hello
    send_jsonl(sock, {"type": "hello_ack", "client": "ci-test", "protocol": "jsonl"})

    # list_ifaces should include vcan0
    send_jsonl(sock, {"type": "list_ifaces"})
    ifaces = recv_jsonl(sock, timeout_s=5.0)
    assert ifaces["type"] == "ifaces", ifaces
    logging.info("ifaces=%s", ifaces["items"])
    assert IFACE in ifaces["items"], f"{IFACE} not found in {ifaces['items']}"

    # subscribe to vcan0
    send_jsonl(sock, {"type": "subscribe", "ifaces": [IFACE]})
    sub = recv_jsonl(sock, timeout_s=5.0)
    assert sub["type"] == "subscribed", sub

    # send a frame on vcan0 using cansend
    # 0x123 with payload DEADBEEF
    logging.info("sending frame via cansend on %s", IFACE)
    subprocess.check_call(["cansend", IFACE, "123#DEADBEEF"])

    # Expect a frame event
    deadline = time.time() + 5.0
    got = False
    while time.time() < deadline:
        msg = recv_jsonl(sock, timeout_s=5.0)
        if msg.get("type") == "frame" and msg.get("iface") == IFACE:
            # data_hex might include more bytes depending on your generator; check it contains deadbeef
            data_hex = msg.get("data_hex", "").lower()
            if "deadbeef" in data_hex:
                got = True
                break

    assert got, "did not observe rx frame event containing deadbeef"

    # unsubscribe
    send_jsonl(sock, {"type": "unsubscribe"})
    unsub = recv_jsonl(sock, timeout_s=5.0)
    assert unsub["type"] == "unsubscribed", unsub

    sock.close()
    logging.info("OK - integration test passed")


if __name__ == "__main__":
    main()

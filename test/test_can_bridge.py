import json
import logging
import socket
import time

logging.basicConfig(
    level=logging.DEBUG, format="PY %(asctime)s %(levelname)s %(message)s"
)


def send_jsonl(sock: socket.socket, obj: dict):
    line = json.dumps(obj) + "\n"
    logging.debug("-> %s", line.strip())
    sock.sendall(line.encode("utf-8"))


def recv_jsonl(sock: socket.socket) -> dict:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed connection")
        buf += chunk
    line, _rest = buf.split(b"\n", 1)
    text = line.decode("utf-8")
    logging.debug("<- %s", text)
    return json.loads(text)


def main():
    addr = ("127.0.0.1", 9500)
    logging.info("connecting to %s:%s", *addr)
    s = socket.create_connection(addr, timeout=3)

    # Step 2: receive hello from server
    hello = recv_jsonl(s)
    assert hello["type"] == "hello", hello
    logging.info("server hello: %s", hello)

    # Send hello_ack
    send_jsonl(s, {"type": "hello_ack", "client": "py-test", "protocol": "jsonl"})

    # Ping/pong still works
    send_jsonl(s, {"type": "ping", "id": 1})
    resp = recv_jsonl(s)
    assert resp["type"] == "pong" and resp["id"] == 1, resp
    logging.info("OK ping/pong after handshake")

    time.sleep(0.2)
    s.close()
    logging.info("closed")


if __name__ == "__main__":
    main()

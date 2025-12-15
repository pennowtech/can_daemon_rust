import json
import logging
import socket
import time

logging.basicConfig(
    level=logging.DEBUG, format="PY %(asctime)s %(levelname)s %(message)s"
)


class JsonlClient:
    def __init__(self, host="127.0.0.1", port=9500):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.buf = b""

    def send(self, obj):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        logging.info("-> %s", obj)
        self.sock.sendall(line)

    def recv(self):
        # Keep data after newline for next call (critical for streaming)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("server closed")
            self.buf += chunk

        line, self.buf = self.buf.split(b"\n", 1)
        obj = json.loads(line.decode("utf-8"))
        logging.info("<- %s", obj)
        return obj

    def close(self):
        self.sock.close()


def main():
    client = JsonlClient()

    hello = client.recv()
    assert hello["type"] == "hello", hello
    client.send({"type": "hello_ack", "client": "py-test", "protocol": "jsonl"})

    client.send({"type": "list_ifaces"})
    resp = client.recv()
    assert resp["type"] == "ifaces", resp
    logging.info("ifaces=%s", resp["items"])

    client.send({"type": "ping", "id": 9})
    pong = client.recv()
    assert pong["type"] == "pong" and pong["id"] == 9, pong

    time.sleep(0.2)

    logging.info("Subscribing  to read can packets from iface = %s", resp["items"][0])

    client.send({"type": "subscribe", "ifaces": [resp["items"][0]]})
    resp = client.recv()
    assert resp["type"] == "subscribed"

    # Send CAN frames
    logging.info("Sending can packets...")
    client.send(
        {
            "type": "send_frame",
            "iface": resp["ifaces"][0],
            "id": 0x123,
            "is_fd": False,
            "data_hex": "aeadbeef",
        }
    )
    resp = client.recv()
    assert resp["type"] == "send_ack" and resp["ok"] is True

    # read frames for 2 seconds
    # Wait for a frame event (rx or tx event)
    deadline = time.time() + 12.0
    got = False
    n = 0
    while time.time() < deadline:
        msg = client.recv()
        if msg["type"] == "frame" and msg["iface"] == "can0":
            got = True
            n += 1

    assert got, "did not receive frame event after send_frame"

    logging.info("received %d frames in 2s (%.1f fps)", n, n / 2.0)

    client.send({"type": "unsubscribe"})
    resp = client.recv()
    assert resp["type"] == "unsubscribed"

    client.close()


if __name__ == "__main__":
    main()

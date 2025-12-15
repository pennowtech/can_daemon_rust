import json
import logging
import select
import socket
import time

logging.basicConfig(
    level=logging.DEBUG, format="PY %(asctime)s %(levelname)s %(message)s"
)


class JsonlClient:
    def __init__(self, host="127.0.0.1", port=9500):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.sock.setblocking(False)
        self.buf = b""
        self.frames = []

    def send(self, obj):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        logging.info("-> %s", obj)
        self.sock.sendall(line)

    def poll(self, timeout=0.2):
        """
        Wait up to `timeout` seconds for data.
        Returns decoded JSON object when a full line is available, else None.
        """
        # If we already have a full line buffered, return immediately.
        if b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            obj = json.loads(line.decode("utf-8"))
            logging.info("<- %s", obj)
            return obj

        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return None  # nothing ready

        try:
            chunk = self.sock.recv(4096)
        except BlockingIOError:
            return None

        if not chunk:
            raise RuntimeError("server closed")

        self.buf += chunk

        if b"\n" not in self.buf:
            return None

        line, self.buf = self.buf.split(b"\n", 1)
        obj = json.loads(line.decode("utf-8"))
        logging.info("<- %s", obj)
        return obj

    def recv(self, timeout=3.0):
        """
        Wait up to timeout for a single message. Raises TimeoutError if none arrives.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.poll(timeout=0.2)
            if msg is not None:
                return msg
        raise TimeoutError("timed out waiting for message")

    def recv_until(self, want_type, timeout=3.0, keep_frames=True):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.poll(timeout=0.2)
            if msg is None:
                continue

            msg_type = msg.get("type")
            if msg_type == want_type:
                return msg

            if msg_type == "frame":
                if keep_frames:
                    self.frames.append(msg)
                continue

            logging.debug("Ignoring while waiting for %s: %s", want_type, msg)

        raise TimeoutError(f"timed out waiting for {want_type}")

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
    logging.info("Sending can-classic packets...")
    client.send(
        {
            "type": "send_frame",
            "iface": resp["ifaces"][0],
            "id": 0x123,
            "is_fd": False,
            "data_hex": "aeadbeef",
        }
    )
    # We won't use client.recv() as there may be frame events interleaved.
    # Instead, we use recv_until to skip any frame events that may arrive
    resp = client.recv_until("send_ack", timeout=3)
    assert resp["type"] == "send_ack" and resp["ok"] is True, resp

    logging.info("Sending classic can packets with BRS true. Should fail...")
    client.send(
        {
            "type": "send_frame",
            "iface": "can0",
            "id": 291,
            "is_fd": False,
            "brs": True,
            "esi": False,
            "data_hex": "deadbeef",
        }
    )
    resp = client.recv_until("send_ack", timeout=3)
    assert resp["type"] == "send_ack" and resp["ok"] is False, resp

    logging.info("Sending can-fd packets with BRS true. Should succeed...")
    client.send(
        {
            "type": "send_frame",
            "iface": "can0",
            "id": 291,
            "is_fd": True,
            "brs": True,
            "esi": False,
            "data_hex": "00" * 32,
        }
    )
    resp = client.recv_until("send_ack", timeout=3)
    assert resp["type"] == "send_ack" and resp["ok"] is True, resp

    # read frames for 2 seconds
    # Wait for a frame event (rx or tx event)
    deadline = time.time() + 12.0
    got = False
    n = 0
    while time.time() < deadline:
        msg = client.poll(timeout=0.25)  # non-blocking-ish
        if msg is None:
            continue
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

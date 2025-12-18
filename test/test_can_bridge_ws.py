import json
import logging
import queue
import threading
import time
from typing import Optional

import websocket  # websocket-client

WS_URL = "ws://127.0.0.1:9501/ws"

UNARY_TIMEOUT = 3.0
WAIT_FOR_FRAMES_SECONDS = 10.0


# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger("ws-test")
logger.setLevel(logging.DEBUG)
_handler = logging.StreamHandler()
_handler.setLevel(logging.DEBUG)
_handler.setFormatter(logging.Formatter("PY %(asctime)s %(levelname)s %(message)s"))
logger.handlers[:] = [_handler]


# -----------------------------
# Helpers
# -----------------------------
class TestFailure(Exception):
    pass


def assert_true(cond, msg):
    if not cond:
        raise TestFailure(msg)


def assert_eq(a, b, msg):
    if a != b:
        raise TestFailure(f"{msg}: {a} != {b}")


def fmt_id_hex(can_id: int) -> str:
    return f"0x{can_id:08X}"


def log_expected_traffic(iface: str, seconds: float):
    logger.warning(
        "EXPECTING CAN TRAFFIC on iface='%s'. Waiting up to %.1f seconds for frames...",
        iface,
        seconds,
    )
    logger.warning(
        "This test will ALSO generate traffic over WebSocket using send_frame."
    )


def ws_send(ws: websocket.WebSocket, obj: dict):
    raw = json.dumps(obj)
    logger.debug("WS -> %s", raw)
    ws.send(raw)


def ws_recv_json(ws: websocket.WebSocket, timeout: float) -> dict:
    ws.settimeout(timeout)
    raw = ws.recv()
    logger.debug("WS <- %s", raw)
    return json.loads(raw)


def connect_ws() -> websocket.WebSocket:
    ws = websocket.create_connection(WS_URL, timeout=UNARY_TIMEOUT)

    # Server hello first
    hello = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(hello.get("type"), "hello", "expected hello")

    # client hello_ack
    ws_send(ws, {"type": "hello_ack", "client": "py-ws-test", "protocol": "json"})
    return ws


def pick_iface(ws: websocket.WebSocket) -> Optional[str]:
    ws_send(ws, {"type": "list_ifaces"})
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    if resp.get("type") != "ifaces":
        logger.warning("unexpected list_ifaces response: %s", resp)
        return None

    items = list(resp.get("items", []))
    logger.info("list_ifaces => %s", items)

    if not items:
        return None
    if "vcan0" in items:
        return "vcan0"
    if "can0" in items:
        return "can0"
    return items[0]


def hex_bytes(n: int) -> str:
    return "00" * n


# -----------------------------
# Streaming reader thread
# -----------------------------
def drain_ws_in_thread(
    ws: websocket.WebSocket,
    stop_event: threading.Event,
    out_q: queue.Queue,
    tag: str = "ws",
):
    """
    Continuously receives WS messages and:
      - logs frames immediately
      - pushes ("frame", msg) for frame messages
      - pushes ("msg", msg) for non-frame messages (useful for debugging)
      - pushes ("error", exc) on errors
    """
    logger.debug("[%s] ws reader thread started", tag)
    try:
        ws.settimeout(0.5)
        while not stop_event.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if raw is None:
                raise RuntimeError("ws closed")

            try:
                msg = json.loads(raw)
            except Exception:
                logger.warning("[%s] non-json message: %r", tag, raw)
                continue

            if msg.get("type") == "frame":
                # Live log on arrival
                logger.info(
                    "[%s] FRAME: ts_ms=%s iface=%s dir=%s id=%s is_fd=%s data_hex=%s",
                    tag,
                    msg.get("ts_ms"),
                    msg.get("iface"),
                    msg.get("dir"),
                    fmt_id_hex(int(msg.get("id", 0))),
                    msg.get("is_fd"),
                    msg.get("data_hex"),
                )
                out_q.put(("frame", msg))
            else:
                out_q.put(("msg", msg))

    except Exception as e:
        # During normal shutdown we may close ws; treat as non-fatal unless tests are waiting.
        logger.debug("[%s] ws reader exiting due to error/close: %s", tag, e)
        out_q.put(("error", e))
    finally:
        logger.debug("[%s] ws reader thread exiting", tag)


def wait_for_frames(out_q: queue.Queue, seconds: float, min_frames: int = 1):
    end = time.time() + seconds
    frames = 0
    sample = []
    while time.time() < end:
        try:
            kind, payload = out_q.get(timeout=0.25)
        except queue.Empty:
            continue

        if kind == "frame":
            frames += 1
            if len(sample) < 5:
                sample.append(payload)
            if frames >= min_frames:
                return frames, sample

        elif kind == "error":
            # Let caller decide if fatal; raise
            raise payload

    return frames, sample


def wait_for_frames_zero(out_q: queue.Queue, seconds: float) -> int:
    end = time.time() + seconds
    frames = 0
    while time.time() < end:
        try:
            kind, payload = out_q.get(timeout=0.25)
        except queue.Empty:
            continue
        if kind == "frame":
            frames += 1
        elif kind == "error":
            # Ignore close errors here; if stream is alive, we just expect 0 frames.
            return frames
    return frames


# -----------------------------
# Auto traffic generator
# -----------------------------
def start_ws_traffic_sender(
    ws: websocket.WebSocket, iface: str, stop_event: threading.Event, tag: str = "txgen"
):
    """
    Sends send_frame messages over WS repeatedly so tests don't rely on external traffic.
    """

    def _run():
        logger.info("[%s] starting ws traffic sender iface=%s", tag, iface)
        payloads = ["deadbeef", "01020304", "aabbccdd", "1122", "00"]
        i = 0
        while not stop_event.is_set():
            data_hex = payloads[i % len(payloads)]
            i += 1
            try:
                ws_send(
                    ws,
                    {
                        "type": "send_frame",
                        "iface": iface,
                        "id": 0x123,
                        "is_fd": False,
                        "brs": False,
                        "esi": False,
                        "data_hex": data_hex,
                    },
                )
                # Expect an ack soon; the reader thread will capture it as "msg"
                time.sleep(0.05)
            except Exception as e:
                logger.warning("[%s] traffic send error: %s", tag, e)
                time.sleep(0.2)
        logger.info("[%s] ws traffic sender stopping", tag)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# -----------------------------
# Tests (mirror gRPC suite)
# -----------------------------
def test_ping(ws):
    logger.info("=== test_ping ===")
    ws_send(ws, {"type": "ping", "id": 7})
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "pong", "expected pong")
    assert_eq(resp.get("id"), 7, "pong id mismatch")
    logger.info("✓ ping OK")


def test_list_ifaces(ws):
    logger.info("=== test_list_ifaces ===")
    ws_send(ws, {"type": "list_ifaces"})
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "ifaces", "expected ifaces response")
    logger.info("✓ list_ifaces OK count=%d", len(resp.get("items", [])))


def test_send_frame_invalid_hex(ws, iface):
    logger.info("=== test_send_frame_invalid_hex === iface=%s", iface)
    ws_send(
        ws,
        {
            "type": "send_frame",
            "iface": iface,
            "id": 0x123,
            "is_fd": False,
            "data_hex": "ZZZZ",
        },
    )
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "send_ack", "expected send_ack")
    assert_true(resp.get("ok") is False, "expected failure for invalid hex")
    logger.info("✓ invalid hex rejected: %s", resp.get("error"))


def test_send_frame_classic_len_overflow(ws, iface):
    logger.info("=== test_send_frame_classic_len_overflow === iface=%s", iface)
    ws_send(
        ws,
        {
            "type": "send_frame",
            "iface": iface,
            "id": 0x123,
            "is_fd": False,
            "data_hex": hex_bytes(9),
        },
    )
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "send_ack", "expected send_ack")
    assert_true(resp.get("ok") is False, "expected classic overflow failure")
    logger.info("✓ classic len enforced: %s", resp.get("error"))


def test_send_frame_fd_len_overflow(ws, iface):
    logger.info("=== test_send_frame_fd_len_overflow === iface=%s", iface)
    ws_send(
        ws,
        {
            "type": "send_frame",
            "iface": iface,
            "id": 0x123,
            "is_fd": True,
            "brs": True,
            "data_hex": hex_bytes(65),
        },
    )
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "send_ack", "expected send_ack")
    assert_true(resp.get("ok") is False, "expected fd overflow failure")
    logger.info("✓ fd len enforced: %s", resp.get("error"))


def test_send_frame_success(ws, iface):
    logger.info("=== test_send_frame_success === iface=%s", iface)
    ws_send(
        ws,
        {
            "type": "send_frame",
            "iface": iface,
            "id": 0x321,
            "is_fd": False,
            "data_hex": "deadbeef",
        },
    )
    resp = ws_recv_json(ws, UNARY_TIMEOUT)
    assert_eq(resp.get("type"), "send_ack", "expected send_ack")
    assert_true(resp.get("ok") is True, f"send_frame failed: {resp}")
    logger.info("✓ send_frame OK")


def test_subscribe_and_receive_frames(ws, iface):
    logger.info("=== test_subscribe_and_receive_frames === iface=%s", iface)
    log_expected_traffic(iface, WAIT_FOR_FRAMES_SECONDS)

    # Start reader thread
    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=drain_ws_in_thread, args=(ws, stop_event, out_q, "sub1"), daemon=True
    )
    reader.start()

    # Subscribe
    ws_send(ws, {"type": "subscribe", "ifaces": [iface]})
    # Wait for subscribed ack (arrives in reader thread as "msg", but we can also recv directly if not using reader)
    # We'll wait via queue for a "subscribed"
    subscribed_ok = False
    t_end = time.time() + 2.0
    while time.time() < t_end:
        try:
            kind, payload = out_q.get(timeout=0.25)
        except queue.Empty:
            continue
        if kind == "msg" and payload.get("type") == "subscribed":
            subscribed_ok = True
            break
    assert_true(subscribed_ok, "did not receive subscribed ack")

    # Start traffic generator
    tx_stop = threading.Event()
    tx_thread = start_ws_traffic_sender(ws, iface, tx_stop, tag="txgen1")

    try:
        frames, sample = wait_for_frames(out_q, WAIT_FOR_FRAMES_SECONDS, min_frames=1)
        logger.info(
            "received %d frames (first within %.1fs)", frames, WAIT_FOR_FRAMES_SECONDS
        )
        assert_true(frames > 0, "expected >=1 frame; no traffic received")
    finally:
        tx_stop.set()
        stop_event.set()
        # Closing ws is safest to stop reader; but we keep it open for subsequent tests.
        reader.join(timeout=1.5)
        tx_thread.join(timeout=1.0)

    logger.info("✓ streaming receive OK")


def test_subscribe_filters_ifaces(ws):
    logger.info("=== test_subscribe_filters_ifaces ===")

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=drain_ws_in_thread, args=(ws, stop_event, out_q, "filter"), daemon=True
    )
    reader.start()

    ws_send(ws, {"type": "subscribe", "ifaces": ["nonexistent0"]})

    # Drain a moment for ack
    time.sleep(0.3)

    count = wait_for_frames_zero(out_q, 1.5)
    assert_eq(count, 0, "should receive 0 frames for unknown iface")

    stop_event.set()
    reader.join(timeout=1.0)

    logger.info("✓ iface filtering OK (0 frames)")


def test_subscribe_cancel_by_close():
    """
    WS has no explicit cancel; typical pattern is to close connection or unsubscribe.
    We mimic the gRPC cancel test by closing the WS and ensuring no hang.
    """
    logger.info("=== test_subscribe_cancel_by_close ===")

    ws = connect_ws()
    iface = pick_iface(ws) or "vcan0"

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=drain_ws_in_thread, args=(ws, stop_event, out_q, "cancel"), daemon=True
    )
    reader.start()

    ws_send(ws, {"type": "subscribe", "ifaces": [iface]})

    # Close after a short delay
    time.sleep(0.5)
    stop_event.set()
    ws.close()
    reader.join(timeout=2.0)

    assert_true(not reader.is_alive(), "ws reader did not exit after close")
    logger.info("✓ ws cancel/close OK")


def test_parallel_subscriptions(iface):
    logger.info("=== test_parallel_subscriptions === iface=%s", iface)
    log_expected_traffic(iface, 3.0)

    # Each parallel subscription uses its own WS connection (recommended).
    results = [0, 0]
    errors = []

    def run_one(idx):
        nonlocal results, errors
        try:
            ws = connect_ws()

            stop_event = threading.Event()
            out_q: queue.Queue = queue.Queue()
            reader = threading.Thread(
                target=drain_ws_in_thread,
                args=(ws, stop_event, out_q, f"par{idx}"),
                daemon=True,
            )
            reader.start()

            ws_send(ws, {"type": "subscribe", "ifaces": [iface]})

            tx_stop = threading.Event()
            tx_thread = start_ws_traffic_sender(
                ws, iface, tx_stop, tag=f"txgen-par{idx}"
            )

            frames, _ = wait_for_frames(out_q, 3.0, min_frames=1)
            results[idx] = frames

            tx_stop.set()
            stop_event.set()
            ws.close()
            reader.join(timeout=2.0)
            tx_thread.join(timeout=1.0)

        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=run_one, args=(0,), daemon=True)
    t2 = threading.Thread(target=run_one, args=(1,), daemon=True)

    t1.start()
    t2.start()
    t1.join(timeout=8.0)
    t2.join(timeout=8.0)

    assert_true(not errors, f"errors in parallel subs: {errors}")
    assert_true(
        results[0] > 0 and results[1] > 0,
        f"both subscribers should see frames; got {results}",
    )

    logger.info("✓ parallel subscribers OK: %s", results)


def test_backpressure_behavior(ws, iface):
    logger.info("=== test_backpressure_behavior === iface=%s", iface)
    log_expected_traffic(iface, 3.0)

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=drain_ws_in_thread, args=(ws, stop_event, out_q, "bp"), daemon=True
    )
    reader.start()

    ws_send(ws, {"type": "subscribe", "ifaces": [iface]})

    tx_stop = threading.Event()
    tx_thread = start_ws_traffic_sender(ws, iface, tx_stop, tag="txgen-bp")

    received = 0
    start = time.time()
    try:
        while time.time() - start < 3.0:
            try:
                kind, payload = out_q.get(timeout=0.5)
            except queue.Empty:
                continue

            if kind == "frame":
                received += 1
                time.sleep(0.1)  # slow consumer
            elif kind == "error":
                break
    finally:
        tx_stop.set()
        stop_event.set()
        reader.join(timeout=2.0)
        tx_thread.join(timeout=1.0)

    assert_true(received > 0, "should receive some frames under backpressure")
    logger.info("✓ backpressure OK received=%d", received)


# -----------------------------
# Runner
# -----------------------------
def main():
    logger.info("Connecting WS: %s", WS_URL)
    ws = connect_ws()

    iface = pick_iface(ws)
    if iface is None:
        logger.warning("No ifaces returned; defaulting to vcan0 for tests")
        iface = "vcan0"

    failures = 0
    tests = [
        lambda: test_ping(ws),
        lambda: test_list_ifaces(ws),
        lambda: test_send_frame_invalid_hex(ws, iface),
        lambda: test_send_frame_classic_len_overflow(ws, iface),
        lambda: test_send_frame_fd_len_overflow(ws, iface),
        lambda: test_send_frame_success(ws, iface),
        lambda: test_subscribe_and_receive_frames(ws, iface),
        lambda: test_subscribe_filters_ifaces(ws),
        lambda: test_backpressure_behavior(ws, iface),
        lambda: test_subscribe_cancel_by_close(),
        lambda: test_parallel_subscriptions(iface),
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            logger.error("❌ TEST FAILED: %s", e, exc_info=True)

    try:
        ws.close()
    except Exception:
        pass

    if failures == 0:
        logger.info("🎉 ALL WS TESTS PASSED")
    else:
        logger.error("❌ %d WS TEST(S) FAILED", failures)


if __name__ == "__main__":
    # websocket-client can be noisy; optional:
    websocket.enableTrace(False)
    main()

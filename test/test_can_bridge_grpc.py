"""
gRPC Integration Tests for CAN Bridge Daemon
===========================================

This file contains an exhaustive integration test suite for the CAN bridge
daemon’s gRPC interface. It validates both unary RPCs (Ping, ListIfaces, SendFrame)
and server-streaming behavior (Subscribe → FrameEvent stream).

Key characteristics:
- Uses structured logging (console output) instead of print statements.
- Automatically generates CAN traffic using the SendFrame gRPC RPC when
  streaming tests require frames, so manual traffic injection is not needed.
- Verifies correct behavior under normal operation, invalid inputs,
  parallel subscriptions, stream cancellation, and backpressure.
- Logs CAN frames as they are received, including arbitration IDs
  (formatted in hexadecimal).

The tests are designed to be robust and non-blocking:
- Streaming RPCs are consumed in background threads to avoid hangs.
- Stream cancellation (gRPC StatusCode.CANCELLED) is treated as expected behavior.
- Clear diagnostic logs are emitted to help identify daemon, interface,
  or environment issues.

For deterministic results, it is recommended to run the daemon with a virtual
CAN interface (vcan0) or with the --fake option enabled for 'CAN bridge daemon' app.

Example:
  RUST_LOG=info cargo run -- --grpc-bind 127.0.0.1:9502 --fake
  poetry run python test_can_bridge_grpc.py
"""

import logging
import queue
import threading
import time
import traceback

import grpc
import proto.can_bridge_pb2 as pb
import proto.can_bridge_pb2_grpc as stub

GRPC_ADDR = "127.0.0.1:9502"

# Global timeouts
UNARY_TIMEOUT = 3.0
STREAM_SETUP_TIMEOUT = 30.0
STREAM_RUN_SECONDS = 20.0
WAIT_FOR_FRAMES_SECONDS = 10.0

# -----------------------------
# Logging (console + exhaustive)
# -----------------------------
logger = logging.getLogger("grpc-test")
logger.setLevel(logging.DEBUG)

_handler = logging.StreamHandler()
_handler.setLevel(logging.DEBUG)
_handler.setFormatter(logging.Formatter("PY %(asctime)s %(levelname)s %(message)s"))
logger.handlers[:] = [_handler]


def fmt_id_hex(can_id: int) -> str:
    return f"0x{can_id:08X}"  # 8-hex digits, uppercase


def log_expected_traffic(iface: str, seconds: float):
    logger.warning(
        "EXPECTING CAN TRAFFIC on iface='%s'. Waiting up to %.1f seconds for frames...",
        iface,
        seconds,
    )
    logger.warning(
        "This test will ALSO attempt to generate traffic using gRPC SendFrame in a background thread."
    )


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


def rpc_error_details(e: grpc.RpcError) -> str:
    try:
        return f"code={e.code()} details={e.details()} debug={e.debug_error_string()}"
    except Exception:
        return repr(e)


def pick_iface(client) -> str | None:
    """
    Choose an interface that is most likely to work for tests.
    Preference:
      1) vcan0
      2) can0
      3) first iface returned
    """
    try:
        resp = client.ListIfaces(pb.Empty(), timeout=UNARY_TIMEOUT)
        items = list(resp.items)
        logger.info("list_ifaces => %s", items)
        if not items:
            return None
        if "vcan0" in items:
            return "vcan0"
        if "can0" in items:
            return "can0"
        return items[0]
    except grpc.RpcError as e:
        logger.error("ListIfaces failed: %s", rpc_error_details(e))
        return None


def hex_bytes(n: int) -> str:
    """n bytes of 0x00 in hex string"""
    return "00" * n


def drain_stream_in_thread(
    stream, stop_event: threading.Event, out_q: queue.Queue, tag: str = "stream"
):
    """
    Read streaming responses in background.
    - Push ("frame", ev) for each FrameEvent
    - Push ("error", exc) on real errors
    - Treat CANCELLED as normal on shutdown
    - Log frames immediately on arrival
    """
    logger.debug("[%s] stream reader thread started", tag)
    try:
        for ev in stream:
            if stop_event.is_set():
                logger.debug("[%s] stop_event set; exiting stream loop", tag)
                break

            logger.info(
                "[%s] FRAME: ts_ms=%d iface=%s dir=%s id=%s is_fd=%s data_hex=%s",
                tag,
                ev.ts_ms,
                ev.iface,
                ev.dir,
                fmt_id_hex(ev.id),
                ev.is_fd,
                ev.data_hex,
            )

            out_q.put(("frame", ev))

    except grpc.RpcError as e:
        # CANCELLED is expected when we call stream.cancel()
        if e.code() == grpc.StatusCode.CANCELLED:
            logger.debug("[%s] stream cancelled (expected): %s", tag, e.details())
        else:
            logger.error("[%s] stream reader grpc error: %s", tag, rpc_error_details(e))
            out_q.put(("error", e))
    except Exception as e:
        logger.error("[%s] stream reader error: %s", tag, e)
        out_q.put(("error", e))
    finally:
        logger.debug("[%s] stream reader thread exiting", tag)


def start_traffic_sender(
    client, iface: str, stop_event: threading.Event, tag: str = "txgen"
):
    """
    Sends frames via gRPC SendFrame while stop_event is not set.
    This makes streaming tests self-contained (no manual cansend needed).
    """

    def _run():
        logger.info("[%s] starting traffic sender iface=%s", tag, iface)

        # Choose classic by default; if your iface is FD-only, you can flip is_fd=True
        is_fd = False
        payloads = ["deadbeef", "01020304", "aabbccdd", "1122", "00"]

        i = 0
        while not stop_event.is_set():
            data_hex = payloads[i % len(payloads)]
            i += 1

            try:
                resp = client.SendFrame(
                    pb.SendFrameReq(
                        iface=iface,
                        id=0x123,
                        is_fd=is_fd,
                        brs=False,
                        esi=False,
                        data_hex=data_hex,
                    ),
                    timeout=UNARY_TIMEOUT,
                )

                if not resp.ok:
                    # Not fatal instantly, but very important to see why
                    logger.warning(
                        "[%s] SendFrame failed ok=false error=%s", tag, resp.error
                    )
                else:
                    logger.debug("[%s] SendFrame ok", tag)

            except grpc.RpcError as e:
                logger.warning(
                    "[%s] SendFrame RPC error: %s", tag, rpc_error_details(e)
                )

            time.sleep(0.05)  # 20 fps-ish

        logger.info("[%s] traffic sender stopping", tag)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def wait_for_frames(
    out_q: queue.Queue, seconds: float, min_frames: int = 1
) -> tuple[int, list]:
    """
    Wait up to `seconds` seconds for at least `min_frames` frames.
    Returns (count, sample_events).
    Raises if stream thread reports an error.
    """
    end = time.time() + seconds
    count = 0
    sample = []

    while time.time() < end:
        try:
            kind, payload = out_q.get(timeout=0.25)
        except queue.Empty:
            continue

        if kind == "frame":
            count += 1
            if len(sample) < 5:
                sample.append(payload)

            if count >= min_frames:
                # We got what we needed; return immediately.
                return count, sample

        elif kind == "error":
            raise payload

    return count, sample


def wait_for_frames_zero(out_q: queue.Queue, seconds: float) -> int:
    end = time.time() + seconds
    count = 0
    while time.time() < end:
        try:
            kind, payload = out_q.get(timeout=0.25)
        except queue.Empty:
            continue
        if kind == "frame":
            count += 1
        elif kind == "error":
            raise payload
    return count


# -----------------------------
# Test Cases
# -----------------------------
def test_ping(client):
    logger.info("=== test_ping ===")
    resp = client.Ping(pb.PingReq(id=42), timeout=UNARY_TIMEOUT)
    logger.debug("PingResp=%s", resp)
    assert_eq(resp.id, 42, "ping id mismatch")
    logger.info("✓ ping OK")


def test_list_ifaces(client):
    logger.info("=== test_list_ifaces ===")
    resp = client.ListIfaces(pb.Empty(), timeout=UNARY_TIMEOUT)
    logger.debug("IfacesResp=%s", resp)
    # can be empty, but should not error
    logger.info("✓ list_ifaces OK (count=%d)", len(resp.items))


def test_send_frame_invalid_hex(client, iface):
    logger.info("=== test_send_frame_invalid_hex === iface=%s", iface)
    resp = client.SendFrame(
        pb.SendFrameReq(iface=iface, id=0x123, is_fd=False, data_hex="ZZZZ"),
        timeout=UNARY_TIMEOUT,
    )
    logger.debug("SendAck=%s", resp)
    assert_true(not resp.ok, "expected invalid hex to fail")
    assert_true(resp.error != "", "expected error message")
    logger.info("✓ invalid hex rejected: %s", resp.error)


def test_send_frame_classic_len_overflow(client, iface):
    logger.info("=== test_send_frame_classic_len_overflow === iface=%s", iface)
    resp = client.SendFrame(
        pb.SendFrameReq(iface=iface, id=0x123, is_fd=False, data_hex=hex_bytes(9)),
        timeout=UNARY_TIMEOUT,
    )
    logger.debug("SendAck=%s", resp)
    assert_true(not resp.ok, "expected classic len overflow to fail")
    logger.info("✓ classic CAN len enforced: %s", resp.error)


def test_send_frame_fd_len_overflow(client, iface):
    logger.info("=== test_send_frame_fd_len_overflow === iface=%s", iface)
    resp = client.SendFrame(
        pb.SendFrameReq(
            iface=iface, id=0x123, is_fd=True, brs=True, data_hex=hex_bytes(65)
        ),
        timeout=UNARY_TIMEOUT,
    )
    logger.debug("SendAck=%s", resp)
    assert_true(not resp.ok, "expected FD len overflow to fail")
    logger.info("✓ CAN-FD len enforced: %s", resp.error)


def test_send_frame_success(client, iface):
    logger.info("=== test_send_frame_success === iface=%s", iface)

    resp = client.SendFrame(
        pb.SendFrameReq(iface=iface, id=0x321, is_fd=False, data_hex="deadbeef"),
        timeout=UNARY_TIMEOUT,
    )
    logger.debug("SendAck=%s", resp)

    if not resp.ok:
        # SUPER IMPORTANT: log the server-side error string.
        raise TestFailure(f"send_frame should succeed, but failed: {resp.error}")

    logger.info("✓ send_frame OK")


def test_subscribe_and_receive_frames(client, iface):
    logger.info("=== test_subscribe_and_receive_frames === iface=%s", iface)
    log_expected_traffic(iface, WAIT_FOR_FRAMES_SECONDS)

    stream = client.Subscribe(
        pb.SubscribeReq(ifaces=[iface]), timeout=STREAM_SETUP_TIMEOUT
    )

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()

    reader = threading.Thread(
        target=drain_stream_in_thread,
        args=(stream, stop_event, out_q, "sub1"),
        daemon=True,
    )
    reader.start()

    # Start auto traffic
    tx_stop = threading.Event()
    tx_thread = start_traffic_sender(client, iface, tx_stop, tag="txgen1")

    try:
        count, sample = wait_for_frames(out_q, WAIT_FOR_FRAMES_SECONDS, min_frames=1)
        logger.info(
            "received %d frames (first within %.1fs)", count, WAIT_FOR_FRAMES_SECONDS
        )
        assert_true(count > 0, "expected >=1 frame; no traffic received")
    finally:
        tx_stop.set()
        stop_event.set()
        stream.cancel()
        reader.join(timeout=2.0)
        tx_thread.join(timeout=1.0)

    logger.info("✓ streaming receive OK")


def test_subscribe_filters_ifaces(client):
    logger.info("=== test_subscribe_filters_ifaces ===")

    stream = client.Subscribe(
        pb.SubscribeReq(ifaces=["nonexistent0"]), timeout=STREAM_SETUP_TIMEOUT
    )

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()

    reader = threading.Thread(
        target=drain_stream_in_thread,
        args=(stream, stop_event, out_q, "filter"),
        daemon=True,
    )
    reader.start()

    try:
        count = wait_for_frames_zero(out_q, 1.5)
        assert_eq(count, 0, "should receive 0 frames for unknown iface")
    finally:
        stop_event.set()
        stream.cancel()
        reader.join(timeout=2.0)

    logger.info("✓ iface filtering OK (0 frames)")


def test_subscribe_cancel(client, iface):
    logger.info("=== test_subscribe_cancel === iface=%s", iface)
    log_expected_traffic(iface, WAIT_FOR_FRAMES_SECONDS)

    stream = client.Subscribe(pb.SubscribeReq(ifaces=[iface]))

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()
    t = threading.Thread(
        target=drain_stream_in_thread,
        args=(stream, stop_event, out_q, "cancel"),
        daemon=True,
    )
    t.start()

    try:
        count, _ = wait_for_frames(out_q, WAIT_FOR_FRAMES_SECONDS, min_frames=1)
        logger.info("pre-cancel received frames=%d (at least one arrived)", count)
    finally:
        logger.info("cancelling stream now...")
        stop_event.set()
        stream.cancel()
        t.join(timeout=2.0)

    assert_true(not t.is_alive(), "stream reader thread did not exit after cancel")
    logger.info("✓ subscribe cancel OK")


def test_parallel_subscriptions(client, iface):
    logger.info("=== test_parallel_subscriptions === iface=%s", iface)
    log_expected_traffic(iface, 3.0)

    # Start shared traffic generator
    tx_stop = threading.Event()
    tx_thread = start_traffic_sender(client, iface, tx_stop, tag="txgen-par")

    results = [0, 0]
    errors = []

    def run_sub(idx):
        nonlocal results, errors
        try:
            stream = client.Subscribe(
                pb.SubscribeReq(ifaces=[iface]), timeout=STREAM_SETUP_TIMEOUT
            )
            stop_event = threading.Event()
            out_q: queue.Queue = queue.Queue()

            reader = threading.Thread(
                target=drain_stream_in_thread,
                args=(stream, stop_event, out_q, f"par{idx}"),
                daemon=True,
            )
            reader.start()

            count, _ = wait_for_frames(out_q, 3.0, min_frames=1)
            results[idx] = count

            stop_event.set()
            stream.cancel()
            reader.join(timeout=2.0)

        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=run_sub, args=(0,), daemon=True)
    t2 = threading.Thread(target=run_sub, args=(1,), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=6.0)
    t2.join(timeout=6.0)

    tx_stop.set()
    tx_thread.join(timeout=1.0)

    assert_true(not errors, f"errors in parallel subs: {errors}")
    assert_true(
        results[0] > 0 and results[1] > 0,
        f"both subscribers should see frames; got {results}",
    )

    logger.info("✓ parallel subscribers OK: %s", results)


def test_backpressure_behavior(client, iface):
    logger.info("=== test_backpressure_behavior === iface=%s", iface)
    log_expected_traffic(iface, 3.0)

    stream = client.Subscribe(
        pb.SubscribeReq(ifaces=[iface]), timeout=STREAM_SETUP_TIMEOUT
    )

    stop_event = threading.Event()
    out_q: queue.Queue = queue.Queue()

    reader = threading.Thread(
        target=drain_stream_in_thread,
        args=(stream, stop_event, out_q, "bp"),
        daemon=True,
    )
    reader.start()

    # Start traffic
    tx_stop = threading.Event()
    tx_thread = start_traffic_sender(client, iface, tx_stop, tag="txgen-bp")

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
                time.sleep(0.1)  # slow processing simulates backpressure
            elif kind == "error":
                raise payload
    finally:
        tx_stop.set()
        stop_event.set()
        stream.cancel()
        reader.join(timeout=2.0)
        tx_thread.join(timeout=1.0)

    assert_true(received > 0, "should receive some frames under backpressure")
    logger.info("✓ backpressure OK: received=%d", received)


# -----------------------------
# Runner
# -----------------------------
def main():
    logger.info("Connecting to gRPC server at %s", GRPC_ADDR)
    channel = grpc.insecure_channel(GRPC_ADDR)
    client = stub.CanBridgeStub(channel)

    # Determine iface once, and log strongly if missing
    iface = pick_iface(client)
    if iface is None:
        logger.warning(
            "No ifaces from server. Streaming tests may fail unless --fake is enabled."
        )
        # We'll still run some tests that don't require a real iface.
        iface = "vcan0"  # best-effort default

    tests = [
        lambda c: test_ping(c),
        lambda c: test_list_ifaces(c),
        lambda c: test_send_frame_invalid_hex(c, iface),
        lambda c: test_send_frame_classic_len_overflow(c, iface),
        lambda c: test_send_frame_fd_len_overflow(c, iface),
        lambda c: test_send_frame_success(c, iface),
        lambda c: test_subscribe_and_receive_frames(c, iface),
        lambda c: test_subscribe_filters_ifaces(c),
        lambda c: test_subscribe_cancel(c, iface),
        lambda c: test_parallel_subscriptions(c, iface),
        lambda c: test_backpressure_behavior(c, iface),
    ]

    failures = 0
    for t in tests:
        try:
            t(client)
        except grpc.RpcError as e:
            failures += 1
            logger.error("❌ gRPC error: %s", rpc_error_details(e))
            logger.error("stack:\n%s", traceback.format_exc())
        except Exception:
            failures += 1
            logger.error("❌ TEST FAILED")
            logger.error("stack:\n%s", traceback.format_exc())

    if failures == 0:
        logger.info("🎉 ALL TESTS PASSED")
    else:
        logger.error("❌ %d TEST(S) FAILED", failures)


if __name__ == "__main__":
    main()

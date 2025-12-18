# CAN bridge daemon - gRPC Exhaustive Integration Test Suite

Please note that this README currently covers only gRPC tests. At the time
of this writing, only gRPC is supported. In the future, it's planned to
support tests for Websockets and TCP interfaces as well. The behavior across
all transport methods should be identical, so this README should provide
sufficient understanding for other types of tests, even if it isn't updated
to include them.

## Setup and Testing Instructions

```shell
poetry install --no-root
```

For gRPC tests, first generate the gRPC code:

```shell
poetry run python -m grpc_tools.protoc -I.. --python_out=. --grpc_python_out=. proto/can_bridge.proto 
poetry run python test/test_can_bridge_grpc.py
```

To run TCP and WebSocket tests (once implemented), use:

```shell
poetry run python test/test_can_bridge_tcp.py
poetry run python test/test_can_bridge_ws.py
```

---
## Design intent and philosophy

The _can bridge daemon_ is structured with a core application service (BridgeService)
and separate transport adapters (TCP/WS/gRPC). This test suite targets the gRPC
adapter specifically, but the same functional behavior should match TCP and WebSocket transports.

> If these tests pass on gRPC, they validate the core domain + app logic and the gRPC transport wiring together.

## Purpose

This file is an exhaustive integration test suite for the CAN bridge daemon's gRPC interface.
It validates both:

1. Unary RPC correctness (request/response APIs)
2. Streaming behavior (Subscribe → continuous stream of FrameEvent messages)

The tests are designed to be self-contained and diagnostic-friendly:

* They use structured logging (console output) instead of print statements.
* They aggressively log what they are waiting for, what they received, and why failures happen.
* They automatically generate CAN traffic through the daemon using the SendFrame gRPC RPC
  when a test requires frames to arrive (so you do not need to manually run cansend).

## What is being tested

The suite covers these categories:

A) Connectivity / health

* Ping: verifies basic reachability and server responsiveness.

B) Discovery

* ListIfaces: checks that the daemon returns a list of CAN interfaces it can see.
  This is used to choose a "best" interface for the rest of the tests.

C) Input validation (negative tests)

* SendFrame invalid hex payload is rejected.
* Classic CAN (is_fd=false) rejects payload > 8 bytes.
* CAN-FD (is_fd=true) rejects payload > 64 bytes.
* Optional FD-only flags (brs/esi) may be validated depending on server behavior.

D) Sending frames (TX path)

* SendFrame success: sends a small frame on the selected interface and expects ok=true.
* If sending fails, the test logs the returned error message verbosely to help debugging
  (e.g., interface down, permission issues, socket open failure, no bus, etc.).

E) Streaming frames (RX + bus + filtering)

* SubscribeAndReceiveFrames: subscribes to a chosen interface and expects frames to arrive.
* SubscribeFiltersIfaces: subscribes to a bogus interface and expects zero frames.
* ParallelSubscriptions: opens multiple subscriptions simultaneously and verifies both
  receive frames (verifies fan-out behavior).
* BackpressureBehavior: processes frames slowly to stress buffering/backpressure handling.
* SubscribeCancel: cancels a subscription and verifies reader threads exit cleanly.

## How streaming is handled (important)

gRPC server-streaming iterators can block waiting for messages.
To avoid hangs and to support immediate logging on frame arrival, this suite reads each
subscription stream in a dedicated background thread:

* The reader thread iterates the gRPC stream and logs each received frame immediately.
* Frames are also pushed into a Queue for the main thread to perform assertions.
* When a test ends, it cancels the stream and signals the reader thread to exit.
* gRPC StatusCode.CANCELLED is treated as normal during shutdown (not a failure),
  because cancelling a stream is expected behavior.

## Traffic generation strategy

Many environments have quiet buses (no CAN frames), which would make streaming tests fail.
To make tests reliable, streaming-related tests start a "traffic sender" background thread
that repeatedly calls SendFrame(...) while the test is running.

This uses the daemon itself to generate traffic and ensures:

* The TX path is exercised (gRPC → daemon → SocketCAN TX adapter)
* The RX/streaming path is exercised (SocketCAN RX adapter → event bus → gRPC stream)

Important: on real physical CAN interfaces (e.g., can0), TX may not loop back to RX
unless the bus is correctly wired and another node ACKs frames. For deterministic tests,
use a virtual CAN interface (vcan0).

## Expected environment / prerequisites

1. The CAN bridge daemon must be running with gRPC enabled, e.g.:
   RUST_LOG=info cargo run -- --grpc-bind 127.0.0.1:9502 --fake

   Notes:

   * --fake is optional, but strongly recommended for deterministic streaming tests.
   * Without --fake, you must have actual CAN traffic on the chosen interface.

2. Python dependencies:

   * grpcio
   * grpcio-tools (only needed to generate the *_pb2.py files)

3. Protobuf stubs must be generated (once, from `test` folder): Steps to do that is mentioned in the beginning.

## Configuration knobs

This file usually defines a few constants near the top:

* GRPC_ADDR: gRPC server address (host:port)
* UNARY_TIMEOUT: deadline for unary RPCs
* STREAM_SETUP_TIMEOUT: deadline for Subscribe RPC setup
* WAIT_FOR_FRAMES_SECONDS: how long to wait for at least one frame in streaming tests

## Output / logging format

All output is emitted through Python's logging framework and printed on the console.
Frames are logged as they arrive, with arbitration ID formatted in hexadecimal.

If a test fails, the suite logs:

* the full traceback
* gRPC error details (StatusCode + details + debug string) when available
* any server-provided error strings from SendAck

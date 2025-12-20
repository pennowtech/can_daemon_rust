# CAN Bridge Daemon

The CAN Bridge Daemon exposes Linux SocketCAN interfaces (`can0`, `vcan0`, etc.)
over multiple network protocols so that **local or remote clients** can:

## Features:

- List CAN interfaces
- Send CAN / CAN-FD frames
- Subscribe to RX/TX frame events
- Monitor and debug CAN traffic remotely
- basic health check (`ping` → `pong`)

## It is designed to be:

- transport-agnostic
- efficient for high-rate CAN traffic
- easy to integrate from Python, Rust, JS, or gRPC clients

## Typical Use Cases

* Remote CAN monitoring dashboards
* Hardware-in-the-loop testing
* Headless CAN gateways
* Distributed automotive tooling
* UI apps (Web / Mobile / Desktop)


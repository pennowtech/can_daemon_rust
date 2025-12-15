#![cfg(target_os = "linux")]

use std::time::{SystemTime, UNIX_EPOCH};

use tracing::{info, warn};

use crate::{
    app::BridgeService,
    domain::frame::{Direction, FrameEvent},
};

/// Start SocketCAN RX readers on the given interfaces.
///
/// Implementation notes:
/// - `socketcan` read_frame() is blocking, so we run each iface in `spawn_blocking`.
/// - We try CAN-FD first (CanFdSocket). If it fails to open, fall back to classic CAN.
/// - On read errors we keep retrying (with a short sleep) instead of crashing the daemon.
pub fn start_socketcan_rx(service: BridgeService, ifaces: Vec<String>) {
    for name in ifaces {
        let iface = name.trim().to_string();
        if iface.is_empty() {
            continue;
        }

        let svc = service.clone();
        tokio::task::spawn_blocking(move || {
            run_one_iface_rx_blocking(svc, iface);
        });
    }
}

fn run_one_iface_rx_blocking(service: BridgeService, iface: String) {
    use socketcan::{CanAnyFrame, CanFdSocket, CanSocket, EmbeddedFrame, Socket};

    // Prefer FD socket; if unavailable (or fails), fallback to classic.
    match CanFdSocket::open(&iface) {
        Ok(sock) => {
            info!(iface=%iface, "SocketCAN RX started (CAN-FD socket)");
            loop {
                match sock.read_frame() {
                    Ok(fd_frame) => {
                        // CanFdSocket::read_frame returns a CanFdFrame.
                        let id = id_to_u32(fd_frame.id());
                        let data = fd_frame.data().to_vec();
                        let ev = FrameEvent {
                            ts_ms: now_ms(),
                            iface: iface.clone(),
                            dir: Direction::Rx,
                            id,
                            is_fd: true,
                            data,
                        };
                        crate::domain::frame::log_frame(&ev);
                        service.publish_frame(ev);
                    }
                    Err(e) => {
                        warn!(iface=%iface, error=%e, "SocketCAN read_frame (FD) failed; retrying");
                        std::thread::sleep(std::time::Duration::from_millis(50));
                    }
                }
            }
        }
        Err(e_fd) => {
            warn!(iface=%iface, error=%e_fd, "Failed to open CAN-FD socket; falling back to classic CAN");

            let sock = match CanSocket::open(&iface) {
                Ok(s) => s,
                Err(e) => {
                    warn!(iface=%iface, error=%e, "Failed to open classic CAN socket; RX disabled for this iface");
                    return;
                }
            };

            info!(iface=%iface, "SocketCAN RX started (classic CAN socket)");
            loop {
                match sock.read_frame() {
                    Ok(frame) => {
                        // CanSocket::read_frame returns CanFrame. Convert to CanAnyFrame so
                        // we have a single code path if we evolve this later.
                        let any = CanAnyFrame::from(frame);

                        match any {
                            CanAnyFrame::Normal(cf) => {
                                let id = id_to_u32(cf.id());
                                let data = cf.data().to_vec();
                                let ev = FrameEvent {
                                    ts_ms: now_ms(),
                                    iface: iface.clone(),
                                    dir: Direction::Rx,
                                    id,
                                    is_fd: false,
                                    data,
                                };
                                crate::domain::frame::log_frame(&ev);
                                service.publish_frame(ev);
                            }
                            other => {
                                // Shouldn't happen in classic path, but safe to ignore.
                                warn!(iface=%iface, frame=?other, "Unexpected non-classic frame on classic socket");
                            }
                        }
                    }
                    Err(e) => {
                        warn!(iface=%iface, error=%e, "SocketCAN read_frame (classic) failed; retrying");
                        std::thread::sleep(std::time::Duration::from_millis(50));
                    }
                }
            }
        }
    }
}

/// Convert socketcan::Id into a raw u32 arbitration id.
///
/// `socketcan::Id` is an enum over standard/extended ids.
/// The enum itself not expose `raw()`/`as_raw()`, but the inner id types do.
fn id_to_u32(id: socketcan::Id) -> u32 {
    match id {
        socketcan::Id::Standard(sid) => sid.as_raw() as u32,
        socketcan::Id::Extended(eid) => eid.as_raw(),
    }
}

/// Get current time in milliseconds since UNIX epoch.
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

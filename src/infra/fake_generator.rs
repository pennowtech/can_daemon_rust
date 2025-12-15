// SPDX-License-Identifier: Apache-2.0
//! fake_generator
//!
//! Layer: Infrastructure
//! Purpose:
//! - TODO: describe this module briefly
//!
//! Notes:
//! - Standard file header. Keep stable to avoid churn.

use std::time::{SystemTime, UNIX_EPOCH};

use tokio::time::{self, Duration};
use tracing::{info, warn};

use crate::{
    app::BridgeService,
    domain::frame::{Direction, FrameEvent},
};

/// Starts a fake frame generator that publishes periodic frames on the event bus.
///
/// Why we do this:
/// - validates subscription + streaming
/// - validates transport write path under continuous data
/// - isolates transport issues before plugging in SocketCAN RX
pub fn start_fake_generator(service: BridgeService, ifaces: Vec<String>) {
    let ifaces = ifaces
        .into_iter()
        .filter(|s| !s.trim().is_empty())
        .collect::<Vec<_>>();

    if ifaces.is_empty() {
        warn!("fake generator started with no ifaces; nothing will be emitted");
        return;
    }

    info!(ifaces=?ifaces, "starting fake frame generator");

    tokio::spawn(async move {
        let mut tick = time::interval(Duration::from_millis(200));
        let mut counter: u64 = 0;

        loop {
            tick.tick().await;
            counter += 1;

            for (idx, iface) in ifaces.iter().enumerate() {
                let ts_ms = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_millis() as u64)
                    .unwrap_or(0);

                // Deterministic payload based on counter and iface index.
                let data = vec![
                    (counter & 0xFF) as u8,
                    ((counter >> 8) & 0xFF) as u8,
                    ((counter >> 16) & 0xFF) as u8,
                    (idx & 0xFF) as u8,
                ];

                let ev = FrameEvent {
                    ts_ms,
                    iface: iface.clone(),
                    dir: Direction::Rx,
                    id: 0x123, // constant id for now
                    is_fd: false,
                    data,
                };

                service.publish_frame(ev);
            }
        }
    });
}

// SPDX-License-Identifier: Apache-2.0
//! service
//!
//! Layer: Application
//! Purpose:
//! - TODO: describe this module briefly
//!
//! Notes:
//! - Standard file header. Keep stable to avoid churn.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use tokio::sync::broadcast;

use crate::{
    domain::{
        frame::FrameEvent,
        protocol::{ClientRequest, ServerResponse},
    },
    ports::{can_tx::CanTxPort, discovery::DiscoveryPort},
};

/// BridgeService = application layer entry point.
/// In later steps this will coordinate:
/// - discovery port
/// - can rx/tx ports
/// - subscriptions
/// - replay sources
#[derive(Clone)]
pub struct BridgeService {
    discovery: Arc<dyn DiscoveryPort>,
    can_tx: Arc<dyn CanTxPort>,
    frames_tx: broadcast::Sender<FrameEvent>,
}

impl std::fmt::Debug for BridgeService {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BridgeService").finish()
    }
}

impl BridgeService {
    /// Create a new service with a broadcast bus for frames.
    pub fn new(discovery: Arc<dyn DiscoveryPort>, can_tx: Arc<dyn CanTxPort>) -> Self {
        // Capacity chosen for dev; tune later. If receivers lag, they’ll drop messages.
        let (frames_tx, _) = broadcast::channel::<FrameEvent>(1024);
        Self {
            discovery,
            can_tx,
            frames_tx,
        }
    }

    /// Subscribe to the internal frame bus.
    /// Each TCP connection gets its own receiver.
    pub fn subscribe_frames(&self) -> broadcast::Receiver<FrameEvent> {
        self.frames_tx.subscribe()
    }

    /// Publish a frame event into the system (used by fake generator now,
    /// and later by SocketCAN RX and replay sources).
    pub fn publish_frame(&self, ev: FrameEvent) {
        // Ignore send errors (only happens if no receivers).
        let _ = self.frames_tx.send(ev);
    }

    /// Handle a single request and return a response.
    /// NOTE: Subscribe/Unsubscribe are handled in the transport layer because they
    /// are per-connection state. (Transport replies with Subscribed/Unsubscribed.)
    pub async fn handle(&self, req: ClientRequest) -> ServerResponse {
        match req {
            ClientRequest::Ping { id } => ServerResponse::Pong { id },
            ClientRequest::ListIfaces => match self.discovery.list_can_ifaces().await {
                Ok(items) => ServerResponse::Ifaces { items },
                Err(e) => ServerResponse::Error {
                    message: format!("list_ifaces failed: {e}"),
                },
            },

            ClientRequest::HelloAck { .. } => ServerResponse::Error {
                message: "hello_ack is handled by transport layer".to_string(),
            },

            ClientRequest::Subscribe { .. } | ClientRequest::Unsubscribe => ServerResponse::Error {
                message: "subscribe/unsubscribe are handled by transport layer".to_string(),
            },

            ClientRequest::SendFrame {
                iface,
                id,
                is_fd,
                brs,
                esi,
                data_hex,
            } => {
                let data = match hex_to_bytes(&data_hex) {
                    Ok(v) => v,
                    Err(e) => {
                        return ServerResponse::SendAck {
                            ok: false,
                            error_message: Some(format!("bad data_hex: {e}")),
                        }
                    }
                };

                if let Err(e) = self
                    .can_tx
                    .send(iface.clone(), id, is_fd, brs, esi, data.clone())
                    .await
                {
                    return ServerResponse::SendAck {
                        ok: false,
                        error_message: Some(e.to_string()),
                    };
                }

                // Publish TX event so subscribers can see outgoing frames too (useful for UI).
                // Timestamp set by transport layer in Step 6; here we can leave it to
                // the socketcan adapter later. For now, we publish with ts_ms=0 (or you can set now_ms).
                self.publish_frame(FrameEvent {
                    ts_ms: now_ms(),
                    iface,
                    dir: crate::domain::frame::Direction::Tx,
                    id,
                    is_fd,
                    data,
                });

                ServerResponse::SendAck {
                    ok: true,
                    error_message: None,
                }
            }
        }
    }
}

/// Decode hex string (accepts upper/lower, optional spaces).
fn hex_to_bytes(s: &str) -> anyhow::Result<Vec<u8>> {
    use anyhow::{anyhow, bail};

    let filtered: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    if filtered.len() % 2 != 0 {
        bail!("hex string must have even length");
    }

    let mut out = Vec::with_capacity(filtered.len() / 2);
    let bytes = filtered.as_bytes();

    fn val(b: u8) -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(10 + (b - b'a')),
            b'A'..=b'F' => Some(10 + (b - b'A')),
            _ => None,
        }
    }

    for i in (0..bytes.len()).step_by(2) {
        let hi = val(bytes[i]).ok_or_else(|| anyhow!("invalid hex char '{}'", bytes[i] as char))?;
        let lo = val(bytes[i + 1])
            .ok_or_else(|| anyhow!("invalid hex char '{}'", bytes[i + 1] as char))?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

/// Get current time in milliseconds since UNIX epoch.
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

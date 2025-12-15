// SPDX-License-Identifier: Apache-2.0
//! protocol
//!
//! Layer: Domain
//! Purpose:
//! - TODO: describe this module briefly
//!
//! Notes:
//! - Standard file header. Keep stable to avoid churn.

use serde::{Deserialize, Serialize};

/// Requests sent *to* the daemon (from clients).
///
/// We use serde's "tag" representation:
/// { "type": "ping", "id": 1 }
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientRequest {
    /// handshake ack sent by client after receiving ServerResponse::Hello
    HelloAck { client: String, protocol: String },

    /// Simple liveness test
    Ping { id: u64 },

    /// ask daemon for available CAN interfaces (stub for now)
    ListIfaces,

    /// subscribe to can frames from specific interfaces
    Subscribe { ifaces: Vec<String> },

    /// stop receiving can frames
    Unsubscribe,

    /// send a CAN/CAN-FD frame
    SendFrame {
        iface: String,
        id: u32,
        is_fd: bool,
        data_hex: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerResponse {
    /// handshake greeting sent immediately on connect
    Hello {
        version: String,
        features: Vec<String>,
        server_name: String,
    },

    /// Ping response
    Pong { id: u64 },

    /// list of interfaces
    Ifaces { items: Vec<String> },

    /// can frame subscription ack
    Subscribed { ifaces: Vec<String> },

    /// can frame unsubscription ack
    Unsubscribed,

    /// streamed can frame event
    Frame {
        ts_ms: u64,
        iface: String,
        dir: String,
        id: u32,
        is_fd: bool,
        data_hex: String,
    },

    // Acknowledgement to CAN frame send
    /// Note: does not guarantee successful transmission on bus
    SendAck {
        ok: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        error_message: Option<String>,
    },

    /// Generic error response (protocol, parsing, etc.)
    Error { message: String },
}

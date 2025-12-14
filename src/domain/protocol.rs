use serde::{Deserialize, Serialize};

/// Requests sent *to* the daemon (from clients).
///
/// We use serde's "tag" representation:
/// { "type": "ping", "id": 1 }
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientRequest {
    HelloAck { client: String, protocol: String },
    Ping { id: u64 },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerResponse {
    Hello {
        version: String,
        features: Vec<String>,
        server_name: String,
    },
    Pong {
        id: u64,
    },
    Error {
        message: String,
    },
}

use crate::domain::protocol::{ClientRequest, ServerResponse};

/// BridgeService = application layer entry point.
/// In later steps this will coordinate:
/// - discovery port
/// - can rx/tx ports
/// - subscriptions
/// - replay sources
#[derive(Debug, Clone, Default)]
pub struct BridgeService {}

impl BridgeService {
    pub fn new() -> Self {
        Self::default()
    }

    /// Handle a single request and return a single response.
    /// Keeping this deterministic and pure-ish makes it easy to test.
    pub fn handle(&self, req: ClientRequest) -> ServerResponse {
        match req {
            ClientRequest::Ping { id } => ServerResponse::Pong { id },
            ClientRequest::HelloAck { .. } => ServerResponse::Error {
                message: "hello_ack is handled by transport".to_string(),
            },
        }
    }
}

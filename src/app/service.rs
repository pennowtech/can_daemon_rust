use std::sync::Arc;

use crate::{
    domain::protocol::{ClientRequest, ServerResponse},
    ports::discovery::DiscoveryPort,
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
}

impl std::fmt::Debug for BridgeService {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BridgeService").finish()
    }
}

impl BridgeService {
    pub fn new(discovery: Arc<dyn DiscoveryPort>) -> Self {
        Self { discovery }
    }

    /// Handle a single request and return a single response.
    /// Keeping this deterministic and pure-ish makes it easy to test.
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
                message: "hello_ack is handled by transport".to_string(),
            },
        }
    }
}

use anyhow::Result;
use futures::future::BoxFuture;

use crate::ports::discovery::DiscoveryPort;

/// Stub discovery implementation (used in Step 3 / as fallback).
#[derive(Debug, Clone, Default)]
pub struct StubDiscovery;

impl StubDiscovery {
    pub fn new() -> Self {
        Self
    }
}

impl DiscoveryPort for StubDiscovery {
    fn list_can_ifaces(&self) -> BoxFuture<'static, Result<Vec<String>>> {
        Box::pin(async move {
            // Keep stable ordering for tests.
            Ok(vec!["vcan0".to_string(), "can0".to_string()])
        })
    }
}

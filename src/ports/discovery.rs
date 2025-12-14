use anyhow::Result;
use futures::future::BoxFuture;

/// Port for discovering CAN interfaces.
/// DiscoveryPort = outbound port (application depends on this).
/// Infrastructure provides implementations.
pub trait DiscoveryPort: Send + Sync {
    fn list_can_ifaces(&self) -> BoxFuture<'static, Result<Vec<String>>>;
}

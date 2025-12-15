use anyhow::Result;
use futures::future::BoxFuture;

/// Outbound port for sending a frame on a specific iface.
///
/// Async (BoxFuture) to avoid async-trait.
pub trait CanTxPort: Send + Sync {
    fn send(
        &self,
        iface: String,
        id: u32,
        is_fd: bool,
        data: Vec<u8>,
    ) -> BoxFuture<'static, Result<()>>;
}


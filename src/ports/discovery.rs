// SPDX-License-Identifier: Apache-2.0
//! discovery
//!
//! Layer: Ports
//! Purpose:
//! - TODO: describe this module briefly
//!
//! Notes:
//! - Standard file header. Keep stable to avoid churn.

use anyhow::Result;
use futures::future::BoxFuture;

/// Port for discovering CAN interfaces.
/// DiscoveryPort = outbound port (application depends on this).
/// Infrastructure provides implementations.
pub trait DiscoveryPort: Send + Sync {
    fn list_can_ifaces(&self) -> BoxFuture<'static, Result<Vec<String>>>;
}

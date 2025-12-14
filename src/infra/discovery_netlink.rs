use anyhow::{Context, Result};
use futures::{future::BoxFuture, TryStreamExt};
use tracing::{debug, info};

use crate::ports::discovery::DiscoveryPort;

/// Linux netlink-based interface discovery.
#[derive(Debug, Clone, Default)]
pub struct NetlinkDiscovery;

impl NetlinkDiscovery {
    pub fn new() -> Self {
        Self
    }

    fn is_can_like(name: &str) -> bool {
        name.starts_with("can") || name.starts_with("vcan") || name.starts_with("slcan")
    }
}

impl DiscoveryPort for NetlinkDiscovery {
    fn list_can_ifaces(&self) -> BoxFuture<'static, Result<Vec<String>>> {
        Box::pin(async move {
            let (conn, handle, _) =
                rtnetlink::new_connection().context("create rtnetlink connection")?;

            // Drive the connection in background on the *existing* runtime.
            tokio::spawn(conn);

            let mut links = handle.link().get().execute();

            let mut out: Vec<String> = Vec::new();

            while let Some(msg) = links.try_next().await.context("netlink get links")? {
                let mut name: Option<String> = None;

                // IMPORTANT:
                // Your `LinkMessage` uses `attributes` (not `nlas`).
                // And the attribute enum type comes from *rtnetlink’s* dependency version.
                // Therefore we must match using the attribute type from the message itself.
                for attr in msg.attributes.into_iter() {
                    // This relies on the attribute enum being in the same crate instance as msg.
                    // If you still have netlink-packet-route version skew, remove your direct
                    // netlink-packet-route dependency or pin it to match rtnetlink.
                    if let netlink_packet_route::link::LinkAttribute::IfName(n) = attr {
                        name = Some(n);
                        break;
                    }
                }

                if let Some(ifname) = name {
                    debug!(iface=%ifname, "discovered link");
                    if Self::is_can_like(&ifname) {
                        out.push(ifname);
                    }
                }
            }

            out.sort();
            out.dedup();

            info!(count = out.len(), ifaces=?out, "netlink discovery complete");
            Ok(out)
        })
    }
}

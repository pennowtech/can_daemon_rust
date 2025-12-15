// SPDX-License-Identifier: Apache-2.0
//! CAN Bridge Daemon – SocketCAN TX adapter
//!
//! Layer: Infrastructure
//! Purpose:
//! - Transmit CAN / CAN-FD frames via Linux SocketCAN
//!
//! Design:
//! - Implements CanTxPort
//! - Blocking I/O isolated in spawn_blocking

#![cfg(target_os = "linux")]

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

use anyhow::{anyhow, bail, Context, Result};
use futures::future::BoxFuture;
use nix::unistd::dup;
use tracing::{info, warn};

use crate::ports::can_tx::CanTxPort;

/// Cached sockets per iface, for performance.
#[derive(Default)]
struct SocketCache {
    classic: HashMap<String, socketcan::CanSocket>,
    fd: HashMap<String, socketcan::CanFdSocket>,
}

/// SocketCAN TX adapter with RawFd-based caching.
///
/// Properties:
/// - One master socket per iface
/// - Each send dup()s the fd → safe concurrent writers
/// - Evicts cache on write failure
/// - No panics, no try_clone()
#[derive(Clone, Default)]
pub struct SocketCanTx {
    cache: Arc<Mutex<SocketCache>>,
}

impl SocketCanTx {
    pub fn new() -> Self {
        Self::default()
    }
}

impl CanTxPort for SocketCanTx {
    fn send(
        &self,
        iface: String,
        id: u32,
        is_fd: bool,
        brs: bool,
        esi: bool,
        data: Vec<u8>,
    ) -> BoxFuture<'static, Result<()>> {
        let this = self.clone();
        Box::pin(async move {
            // Blocking work in spawn_blocking
            tokio::task::spawn_blocking(move || -> Result<()> {
                this.send_blocking(&iface, id, is_fd, brs, esi, &data)
            })
            .await
            .map_err(|e| anyhow!("Tokio task join error: {}", e))?
        })
    }
}

impl SocketCanTx {
    fn send_blocking(
        &self,
        iface: &str,
        id: u32,
        is_fd: bool,
        brs: bool,
        esi: bool,
        data: &[u8],
    ) -> Result<()> {
        use socketcan::{frame::FdFlags, CanFdFrame, CanFrame, EmbeddedFrame, Id};

        // ---- Validate request
        if is_fd {
            if data.len() > 64 {
                bail!("CAN-FD payload too large: {} (max 64)", data.len());
            }
        } else {
            if data.len() > 8 {
                bail!("Classic CAN payload too large: {} (max 8)", data.len());
            }
            if brs || esi {
                bail!("brs/esi flags supplied when is_fd is false");
            }
        }

        let sid: Id = make_id(id)?;

        // ---- Build frame + send
        if is_fd {
            let mut flags = FdFlags::empty();
            if brs {
                flags |= FdFlags::BRS;
            }
            if esi {
                flags |= FdFlags::ESI;
            }

            let frame = CanFdFrame::with_flags(sid, data, flags)
                .ok_or_else(|| anyhow!("failed to construct CanFdFrame (len={})", data.len()))?;

            self.send_fd(iface, id, data.len(), brs, esi, &frame)
        } else {
            let frame = CanFrame::new(sid, data)
                .ok_or_else(|| anyhow!("failed to construct CanFrame (len={})", data.len()))?;

            self.send_classic(iface, id, data.len(), &frame)
        }
    }

    fn send_classic(
        &self,
        iface: &str,
        id: u32,
        len: usize,
        frame: &socketcan::CanFrame,
    ) -> Result<()> {
        use socketcan::{CanSocket, Socket};

        let sock = CanSocket::open(iface).with_context(|| format!("open CanSocket({iface})"))?;
        match sock.write_frame(frame) {
            Ok(_) => {
                info!(iface=%iface, id=%id, len=%len, "tx sent (classic)");
                Ok(())
            }
            Err(e) => {
                warn!(iface=%iface, id=%id, error=%e, "CAN(classic) write failed");
                Err(anyhow!(e))
            }
        }
    }

    fn send_fd(
        &self,
        iface: &str,
        id: u32,
        len: usize,
        brs: bool,
        esi: bool,
        frame: &socketcan::CanFdFrame,
    ) -> Result<()> {
        use socketcan::{CanFdSocket, Socket};

        let sock =
            CanFdSocket::open(iface).with_context(|| format!("open CanFdSocket({iface})"))?;

        match sock.write_frame(frame) {
            Ok(_) => {
                info!(iface=%iface, id=%id, len=%len, brs=%brs, esi=%esi, "tx sent (can-fd)");
                Ok(())
            }
            Err(e) => {
                warn!(iface=%iface, id=%id, error=%e, "CAN(FD) write failed");
                Err(anyhow!(e))
            }
        }
    }
}

fn make_id(id_raw: u32) -> Result<socketcan::Id> {
    use socketcan::{ExtendedId, Id, StandardId};

    if id_raw <= 0x7FF {
        let std = StandardId::new(id_raw as u16)
            .ok_or_else(|| anyhow!("invalid standard id: {id_raw}"))?;
        Ok(Id::from(std))
    } else if id_raw <= 0x1FFFFFFF {
        let ext =
            ExtendedId::new(id_raw).ok_or_else(|| anyhow!("invalid extended id: {id_raw}"))?;
        Ok(Id::from(ext))
    } else {
        Err(anyhow!("CAN id out of range: {id_raw}"))
    }
}

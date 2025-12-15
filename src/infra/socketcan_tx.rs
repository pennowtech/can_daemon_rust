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

use anyhow::{anyhow, Context, Result};
use futures::future::BoxFuture;
use tracing::{info, warn};

use crate::ports::can_tx::CanTxPort;

/// SocketCAN TX adapter.
///
/// Uses blocking socketcan writes; performed inside spawn_blocking.
#[derive(Debug, Clone, Default)]
pub struct SocketCanTx;

impl SocketCanTx {
    pub fn new() -> Self {
        Self
    }
}

impl CanTxPort for SocketCanTx {
    fn send(
        &self,
        iface: String,
        id: u32,
        is_fd: bool,
        data: Vec<u8>,
    ) -> BoxFuture<'static, Result<()>> {
        Box::pin(async move {
            // Blocking work in spawn_blocking
            tokio::task::spawn_blocking(move || send_blocking(&iface, id, is_fd, &data))
                .await
                .map_err(|e| anyhow!("join error: {e}"))?
        })
    }
}

fn send_blocking(iface: &str, id: u32, is_fd: bool, data: &[u8]) -> Result<()> {
    use socketcan::{
        frame::FdFlags, CanFdFrame, CanFdSocket, CanFrame, CanSocket, EmbeddedFrame, Id, Socket,
    };

    let sid: Id = make_id(id)?;

    if is_fd {
        let sock =
            CanFdSocket::open(iface).with_context(|| format!("open CanFdSocket({iface})"))?;

        // Use flags=empty for now; we’ll add BRS/ESI in a later step.
        let frame = CanFdFrame::with_flags(sid, data, FdFlags::empty())
            .ok_or_else(|| anyhow!("failed to construct CanFdFrame (len={})", data.len()))?;

        sock.write_frame(&frame)
            .with_context(|| format!("write_frame(fd) iface={iface} id={id}"))?;

        info!(iface=%iface, id=%id, len=data.len(), "tx sent (can-fd)");
        Ok(())
    } else {
        let sock = CanSocket::open(iface).with_context(|| format!("open CanSocket({iface})"))?;

        let frame = CanFrame::new(sid, data)
            .ok_or_else(|| anyhow!("failed to construct CanFrame (len={})", data.len()))?;

        sock.write_frame(&frame)
            .with_context(|| format!("write_frame(classic) iface={iface} id={id}"))?;

        info!(iface=%iface, id=%id, len=data.len(), "tx sent (classic)");
        Ok(())
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

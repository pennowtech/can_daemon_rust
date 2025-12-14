use std::time::Duration;
use tokio::time;

use std::{
    net::SocketAddr,
    sync::atomic::{AtomicU64, Ordering},
};

use anyhow::{Context, Result};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
};
use tracing::{debug, info, warn};

use crate::{
    app::BridgeService,
    domain::protocol::{ClientRequest, ServerResponse},
};

/// TCP server for JSONL (one JSON object per line).
///
/// Clean-architecture note:
/// This is an infrastructure adapter. It:
/// - reads bytes from TCP,
/// - parses into domain request objects,
/// - calls the application service,
/// - serializes a domain response back to bytes.
pub struct TcpJsonlServer {
    addr: SocketAddr,
    next_conn_id: AtomicU64,
}

impl TcpJsonlServer {
    pub fn new(addr: SocketAddr) -> Self {
        Self {
            addr,
            next_conn_id: AtomicU64::new(1),
        }
    }

    pub async fn run(&self, service: BridgeService) -> Result<()> {
        let listener = TcpListener::bind(self.addr)
            .await
            .with_context(|| format!("failed to bind TCP listener on {}", self.addr))?;

        info!(addr=%self.addr, "tcp server listening (jsonl)");

        loop {
            let (stream, peer) = listener.accept().await?;
            let conn_id = self.next_conn_id.fetch_add(1, Ordering::Relaxed);

            info!(%conn_id, %peer, "client connected");

            let svc = service.clone();
            tokio::spawn(async move {
                if let Err(e) = handle_connection(conn_id, peer, stream, svc).await {
                    warn!(%conn_id, %peer, error=%e, "client handler ended");
                } else {
                    info!(%conn_id, %peer, "client handler ended");
                }
            });
        }
    }
}

async fn handle_connection(
    conn_id: u64,
    peer: SocketAddr,
    stream: TcpStream,
    service: BridgeService,
) -> Result<()> {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);

    // Send Hello immediately
    let hello = ServerResponse::Hello {
        version: "0.2".to_string(),
        features: vec!["tcp".into(), "jsonl".into()],
        server_name: "can-bridge-daemon".to_string(),
    };
    write_jsonl(&mut write_half, &hello).await?;

    // Expect hello_ack within 3 seconds
    let mut first = String::new();
    let n = tokio::time::timeout(
        std::time::Duration::from_secs(3),
        reader.read_line(&mut first),
    )
    .await
    .context("hello_ack timeout")??;

    if n == 0 {
        anyhow::bail!("client disconnected before hello_ack");
    }

    let raw = first.trim_end_matches(&['\r', '\n'][..]).to_string();
    let req: ClientRequest = serde_json::from_str(&raw).context("invalid hello_ack json")?;

    match req {
        ClientRequest::HelloAck { client, protocol } => {
            info!(%conn_id, %peer, %client, %protocol, "hello_ack received");
        }
        other => {
            anyhow::bail!("expected hello_ack, got: {:?}", other);
        }
    }

    // Main loop
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            // EOF (client disconnected)
            debug!(%conn_id, %peer, "client EOF");
            return Ok(());
        }

        let raw = line.trim_end_matches(&['\r', '\n'][..]).to_string();
        debug!(%conn_id, %peer, raw=%raw, "rx line");

        // Parse request
        let req: ClientRequest = match serde_json::from_str(&raw) {
            Ok(v) => v,
            Err(e) => {
                warn!(%conn_id, %peer, error=%e, raw=%raw, "bad json request");
                let resp = ServerResponse::Error {
                    message: format!("invalid json request: {e}"),
                };
                write_jsonl(&mut write_half, &resp).await?;
                continue;
            }
        };

        debug!(%conn_id, %peer, req=?req, "parsed request");

        // Call application service
        let resp = service.handle(req);

        debug!(%conn_id, %peer, resp=?resp, "sending response");
        write_jsonl(&mut write_half, &resp).await?;
    }
}

/// Serialize and write one JSONL response.
async fn write_jsonl<W: AsyncWriteExt + Unpin>(w: &mut W, msg: &ServerResponse) -> Result<()> {
    let mut s = serde_json::to_string(msg)?;
    s.push('\n');
    w.write_all(s.as_bytes()).await?;
    w.flush().await?;
    Ok(())
}

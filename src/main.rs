use anyhow::Result;
use clap::Parser;
use tracing::{info, warn};

mod app;
mod domain;
mod infra;

/// CLI arguments (Step 0/1).
/// Keep this small and extend later.
#[derive(Parser, Debug, Clone)]
#[command(author, version, about)]
struct Args {
    /// TCP bind address for JSONL protocol, e.g. 127.0.0.1:9500
    #[arg(long, default_value = "127.0.0.1:9500")]
    bind: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Structured logs; can be controlled with:
    // RUST_LOG=info or RUST_LOG=debug
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let args = Args::parse();
    info!(bind=%args.bind, "can_bridge_daemon starting (step 0/1)");

    // Build application service (use-case layer).
    let service = app::BridgeService::new();

    // Start TCP server (infrastructure adapter).
    let server = infra::transport_tcp::TcpJsonlServer::new(args.bind.parse()?);
    let server_handle = tokio::spawn({
        let service = service.clone();
        async move {
            if let Err(e) = server.run(service).await {
                warn!(error=%e, "tcp server exited with error");
            }
        }
    });

    info!("daemon running (terminate with Ctrl+C)");

    // Step 0: stay alive until Ctrl+C.
    tokio::signal::ctrl_c().await?;
    info!("Ctrl+C received; shutting down");

    // For step 1, we just abort the server task on shutdown.
    // Later steps can do graceful draining.
    server_handle.abort();

    Ok(())
}

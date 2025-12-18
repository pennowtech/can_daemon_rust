use std::{net::SocketAddr, pin::Pin, sync::Arc};

use anyhow::{Context, Result};
use tokio_stream::{wrappers::BroadcastStream, StreamExt};
use tonic::{Request, Response, Status};
use tracing::{info, warn};

use crate::{
    app::BridgeService,
    domain::{
        frame::{Direction, FrameEvent as DomainFrameEvent},
        protocol::ClientRequest,
    },
};

pub mod pb {
    tonic::include_proto!("canbridge.v1");
}

use pb::can_bridge_server::{CanBridge, CanBridgeServer};

/// gRPC server wrapper
pub struct GrpcServer {
    addr: SocketAddr,
}

impl GrpcServer {
    pub fn new(addr: SocketAddr) -> Self {
        Self { addr }
    }

    pub async fn run(self, service: BridgeService) -> Result<()> {
        let svc = GrpcAdapter {
            service: Arc::new(service),
        };

        info!(addr=%self.addr, "grpc server listening");

        tonic::transport::Server::builder()
            .add_service(CanBridgeServer::new(svc))
            .serve(self.addr)
            .await
            .context("grpc serve failed")?;

        Ok(())
    }
}

/// Adapter implementing protobuf API by calling BridgeService + event bus.
#[derive(Clone)]
struct GrpcAdapter {
    service: Arc<BridgeService>,
}

type SubscribeStream =
    Pin<Box<dyn futures::Stream<Item = Result<pb::FrameEvent, Status>> + Send + 'static>>;

#[tonic::async_trait]
impl CanBridge for GrpcAdapter {
    async fn ping(&self, req: Request<pb::PingReq>) -> Result<Response<pb::PingResp>, Status> {
        let id = req.into_inner().id;
        let resp = self.service.handle(ClientRequest::Ping { id }).await;

        match resp {
            crate::domain::protocol::DaemonResponse::Pong { id } => {
                Ok(Response::new(pb::PingResp { id }))
            }
            crate::domain::protocol::DaemonResponse::Error { message } => {
                Err(Status::internal(message))
            }
            other => Err(Status::internal(format!(
                "unexpected response for ping: {other:?}"
            ))),
        }
    }

    async fn list_ifaces(
        &self,
        _req: Request<pb::Empty>,
    ) -> Result<Response<pb::IfacesResp>, Status> {
        let resp = self.service.handle(ClientRequest::ListIfaces).await;

        match resp {
            crate::domain::protocol::DaemonResponse::Ifaces { items } => {
                Ok(Response::new(pb::IfacesResp { items }))
            }
            crate::domain::protocol::DaemonResponse::Error { message } => {
                Err(Status::internal(message))
            }
            other => Err(Status::internal(format!(
                "unexpected response for list_ifaces: {other:?}"
            ))),
        }
    }

    async fn send_frame(
        &self,
        req: Request<pb::SendFrameReq>,
    ) -> Result<Response<pb::SendAck>, Status> {
        let r = req.into_inner();
        let resp = self
            .service
            .handle(ClientRequest::SendFrame {
                iface: r.iface,
                id: r.id,
                is_fd: r.is_fd,
                brs: r.brs,
                esi: r.esi,
                data_hex: r.data_hex,
            })
            .await;

        match resp {
            crate::domain::protocol::DaemonResponse::SendAck { ok, error_message } => {
                Ok(Response::new(pb::SendAck {
                    ok,
                    error: error_message.unwrap_or_default(), //in proto file, it's error string
                }))
            }
            crate::domain::protocol::DaemonResponse::Error { message } => {
                Ok(Response::new(pb::SendAck {
                    ok: false,
                    error: message,
                }))
            }
            other => Err(Status::internal(format!(
                "unexpected response for send_frame: {other:?}"
            ))),
        }
    }

    type SubscribeStream = SubscribeStream;

    async fn subscribe(
        &self,
        req: Request<pb::SubscribeReq>,
    ) -> Result<Response<Self::SubscribeStream>, Status> {
        let ifaces = req.into_inner().ifaces;
        let iface_set: std::collections::HashSet<String> = ifaces.into_iter().collect();

        let rx = self.service.subscribe_frames();

        // Convert broadcast receiver to a Stream
        // NOTE: BroadcastStream yields Result<T, RecvError>
        let stream = BroadcastStream::new(rx).filter_map(move |item| {
            let iface_set = iface_set.clone();
            match item {
                Ok(ev) => {
                    if iface_set.contains(&ev.iface) {
                        Some(Ok(domain_frame_to_pb(ev)))
                    } else {
                        None
                    }
                }
                Err(e) => {
                    // Lagged: you can choose to drop silently or send an error.
                    // We'll drop and warn.
                    warn!(error=%e, "grpc subscribe lagged/dropped");
                    None
                }
            }
        });

        Ok(Response::new(Box::pin(stream) as Self::SubscribeStream))
    }
}

fn domain_frame_to_pb(ev: DomainFrameEvent) -> pb::FrameEvent {
    pb::FrameEvent {
        ts_ms: ev.ts_ms,
        iface: ev.iface,
        dir: match ev.dir {
            Direction::Rx => "rx".to_string(),
            Direction::Tx => "tx".to_string(),
        },
        id: ev.id,
        is_fd: ev.is_fd,
        data_hex: hex_lower(&ev.data),
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    const LUT: &[u8; 16] = b"0123456789abcdef";
    let mut out = Vec::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(LUT[(b >> 4) as usize]);
        out.push(LUT[(b & 0x0F) as usize]);
    }
    String::from_utf8(out).unwrap_or_default()
}

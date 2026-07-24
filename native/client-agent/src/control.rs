use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};

use crate::config::CONTROL_PROTOCOL_VERSION;

#[derive(Clone, Debug, Default, Serialize)]
pub struct AgentStatus {
    pub ready: bool,
    pub enabled: bool,
    pub heartbeat_unix_ns: u64,
    pub generation: u64,
    pub plan_digest: String,
    pub expected_flows: usize,
    pub active_flows: usize,
    pub expected_listeners: usize,
    pub active_listeners: usize,
    pub packets_sent: u64,
    pub bytes_sent: u64,
    pub reconnects: u64,
    pub protocol_errors: u64,
    pub completed_cycles: u64,
    pub last_cycle_duration_ms: f64,
    pub max_cycle_duration_ms: f64,
    pub last_error: Option<String>,
}

impl AgentStatus {
    pub fn refresh_heartbeat(&mut self) {
        self.heartbeat_unix_ns = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
            .try_into()
            .unwrap_or(u64::MAX);
    }
}

#[derive(Debug, Deserialize)]
pub struct RequestEnvelope {
    pub protocol_version: u32,
    pub op: String,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Debug, Serialize)]
pub struct ResponseEnvelope {
    pub protocol_version: u32,
    pub ok: bool,
    pub status: AgentStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl ResponseEnvelope {
    pub fn ok(status: AgentStatus) -> Self {
        Self {
            protocol_version: CONTROL_PROTOCOL_VERSION,
            ok: true,
            status,
            error: None,
        }
    }

    pub fn error(status: AgentStatus, error: impl Into<String>) -> Self {
        Self {
            protocol_version: CONTROL_PROTOCOL_VERSION,
            ok: false,
            status,
            error: Some(error.into()),
        }
    }
}

pub async fn serve<H, F>(address: SocketAddr, handler: H) -> Result<()>
where
    H: Fn(RequestEnvelope) -> F + Send + Sync + 'static,
    F: std::future::Future<Output = ResponseEnvelope> + Send + 'static,
{
    let listener = TcpListener::bind(address)
        .await
        .with_context(|| format!("bind control endpoint {address}"))?;
    let handler = Arc::new(handler);
    loop {
        let (stream, _) = listener.accept().await?;
        let handler = Arc::clone(&handler);
        tokio::spawn(async move {
            if let Err(error) = handle_connection(stream, handler).await {
                eprintln!("control connection failed: {error:#}");
            }
        });
    }
}

async fn handle_connection<H, F>(stream: TcpStream, handler: Arc<H>) -> Result<()>
where
    H: Fn(RequestEnvelope) -> F + Send + Sync + 'static,
    F: std::future::Future<Output = ResponseEnvelope> + Send + 'static,
{
    let (reader, mut writer) = stream.into_split();
    let mut lines = BufReader::new(reader).lines();
    let Some(line) = lines.next_line().await? else {
        return Ok(());
    };
    let request: RequestEnvelope = serde_json::from_str(&line).context("decode control request")?;
    let response = if request.protocol_version != CONTROL_PROTOCOL_VERSION {
        ResponseEnvelope::error(
            AgentStatus::default(),
            format!(
                "protocol version {} is unsupported; expected {}",
                request.protocol_version, CONTROL_PROTOCOL_VERSION
            ),
        )
    } else {
        handler(request).await
    };
    let mut encoded = serde_json::to_vec(&response)?;
    encoded.push(b'\n');
    writer.write_all(&encoded).await?;
    writer.shutdown().await?;
    Ok(())
}

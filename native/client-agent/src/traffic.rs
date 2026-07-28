use std::collections::BTreeMap;
use std::io;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::os::fd::AsRawFd;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::io::AsyncReadExt;
use tokio::net::{TcpListener, TcpSocket, TcpStream, UdpSocket};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
use tokio::time::{Instant, MissedTickBehavior, interval_at, sleep, timeout};

use crate::config::{ClientAgentConfig, TRAFFIC_CONTROL_PORT};
use crate::control::{self, AgentStatus, RequestEnvelope, ResponseEnvelope};

const TRAFFIC_SCHEDULER_TICK: Duration = Duration::from_millis(100);
const MAX_TCP_WRITE_BYTES: usize = 64 * 1024;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const MIN_RECONNECT: Duration = Duration::from_millis(500);
const MAX_RECONNECT: Duration = Duration::from_secs(5);

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
enum Transport {
    Tcp,
    Udp,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ListenerPlan {
    protocol: Transport,
    port: u16,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FlowPlan {
    flow_id: String,
    protocol: Transport,
    dst_ip: Ipv4Addr,
    dst_port: u16,
    bandwidth_bps: u64,
    payload_bytes: usize,
    tcp_mss: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ClientTrafficPlan {
    listeners: Vec<ListenerPlan>,
    flows: Vec<FlowPlan>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LoadPayload {
    generation: u64,
    plan_digest: String,
    plan: ClientTrafficPlan,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GenerationPayload {
    generation: u64,
    plan_digest: String,
}

struct TrafficRuntime {
    local_ip: Ipv4Addr,
    status: Arc<Mutex<AgentStatus>>,
    plan: Option<ClientTrafficPlan>,
    listener_tasks: Vec<JoinHandle<()>>,
    sender_task: Option<JoinHandle<()>>,
}

pub async fn run(path: &Path) -> Result<()> {
    let config = ClientAgentConfig::load(path)?;
    let local = config.local_client()?;
    let status = Arc::new(Mutex::new(AgentStatus::default()));
    let runtime = Arc::new(Mutex::new(TrafficRuntime {
        local_ip: local.data_ip,
        status: Arc::clone(&status),
        plan: None,
        listener_tasks: Vec::new(),
        sender_task: None,
    }));
    let address = SocketAddr::V4(SocketAddrV4::new(local.management_ip, TRAFFIC_CONTROL_PORT));
    let handler_runtime = Arc::clone(&runtime);
    let server = control::serve(address, move |request| {
        let runtime = Arc::clone(&handler_runtime);
        async move { handle_request(runtime, request).await }
    });
    tokio::select! {
        result = server => result.context("traffic control server"),
        result = tokio::signal::ctrl_c() => {
            result.context("wait for shutdown signal")?;
            let mut runtime = runtime.lock().await;
            runtime.disable().await;
            runtime.stop_listeners().await;
            Ok(())
        },
    }
}

async fn handle_request(
    runtime: Arc<Mutex<TrafficRuntime>>,
    request: RequestEnvelope,
) -> ResponseEnvelope {
    let mut runtime = runtime.lock().await;
    let result = match request.op.as_str() {
        "load_plan" => {
            let payload = serde_json::from_value::<LoadPayload>(request.payload)
                .map_err(|error| error.to_string());
            match payload {
                Ok(payload) => runtime
                    .load(payload)
                    .await
                    .map_err(|error| format!("{error:#}")),
                Err(error) => Err(error),
            }
        }
        "enable" => {
            let payload = serde_json::from_value::<GenerationPayload>(request.payload)
                .map_err(|error| error.to_string());
            match payload {
                Ok(payload) => runtime
                    .enable(payload)
                    .await
                    .map_err(|error| format!("{error:#}")),
                Err(error) => Err(error),
            }
        }
        "disable" => {
            let payload = serde_json::from_value::<GenerationPayload>(request.payload)
                .map_err(|error| error.to_string());
            match payload {
                Ok(payload) => match runtime.validate_generation(&payload).await {
                    Ok(()) => {
                        runtime.disable().await;
                        Ok(())
                    }
                    Err(error) => Err(format!("{error:#}")),
                },
                Err(error) => Err(error),
            }
        }
        "reset" => {
            runtime.reset().await;
            Ok(())
        }
        "status" => Ok(()),
        other => Err(format!("unsupported operation {other}")),
    };
    let snapshot = runtime.status.lock().await.clone();
    match result {
        Ok(()) => ResponseEnvelope::ok(snapshot),
        Err(error) => ResponseEnvelope::error(snapshot, error),
    }
}

impl TrafficRuntime {
    async fn load(&mut self, payload: LoadPayload) -> Result<()> {
        if payload.generation == 0 || payload.plan_digest.is_empty() {
            bail!("generation and plan_digest are required");
        }
        let calculated = plan_digest(&payload.plan)?;
        if calculated != payload.plan_digest {
            bail!(
                "traffic plan digest mismatch: received {}, calculated {}",
                payload.plan_digest,
                calculated
            );
        }
        self.disable().await;
        self.stop_listeners().await;
        let mut handles = Vec::with_capacity(payload.plan.listeners.len());
        for listener in &payload.plan.listeners {
            let result = match listener.protocol {
                Transport::Tcp => spawn_tcp_listener(self.local_ip, listener.port).await,
                Transport::Udp => spawn_udp_listener(self.local_ip, listener.port).await,
            };
            match result {
                Ok(handle) => handles.push(handle),
                Err(error) => {
                    for handle in &handles {
                        handle.abort();
                    }
                    for handle in handles {
                        let _ = handle.await;
                    }
                    self.plan = None;
                    let mut status = self.status.lock().await;
                    *status = AgentStatus::default();
                    status.refresh_heartbeat();
                    return Err(error);
                }
            }
        }
        self.listener_tasks = handles;
        self.plan = Some(payload.plan);
        let mut status = self.status.lock().await;
        status.generation = payload.generation;
        status.plan_digest = payload.plan_digest;
        status.enabled = false;
        status.expected_flows = self.plan.as_ref().map_or(0, |plan| plan.flows.len());
        status.active_flows = 0;
        status.expected_listeners = self.plan.as_ref().map_or(0, |plan| plan.listeners.len());
        status.active_listeners = self.listener_tasks.len();
        status.ready = status.active_listeners == status.expected_listeners;
        status.last_error = None;
        Ok(())
    }

    async fn reset(&mut self) {
        self.disable().await;
        self.stop_listeners().await;
        self.plan = None;
        let mut status = self.status.lock().await;
        *status = AgentStatus::default();
        status.refresh_heartbeat();
    }

    async fn enable(&mut self, payload: GenerationPayload) -> Result<()> {
        self.validate_generation(&payload).await?;
        if self.sender_task.is_some() {
            return Ok(());
        }
        let plan = self.plan.clone().context("traffic plan is not loaded")?;
        {
            let mut status = self.status.lock().await;
            status.enabled = true;
            status.ready = false;
            status.active_flows = 0;
        }
        if plan.flows.is_empty() {
            let mut status = self.status.lock().await;
            status.ready = status.active_listeners == status.expected_listeners;
        } else {
            self.sender_task = Some(tokio::spawn(traffic_scheduler(
                self.local_ip,
                plan.flows,
                Arc::clone(&self.status),
            )));
        }
        Ok(())
    }

    async fn validate_generation(&self, payload: &GenerationPayload) -> Result<()> {
        let status = self.status.lock().await;
        if status.generation != payload.generation || status.plan_digest != payload.plan_digest {
            bail!("traffic generation or plan digest mismatch");
        }
        Ok(())
    }

    async fn disable(&mut self) {
        if let Some(handle) = self.sender_task.take() {
            handle.abort();
            let _ = handle.await;
        }
        let mut status = self.status.lock().await;
        status.enabled = false;
        status.active_flows = 0;
        status.ready = status.active_listeners == status.expected_listeners;
    }

    async fn stop_listeners(&mut self) {
        let handles = self.listener_tasks.drain(..).collect::<Vec<_>>();
        for handle in &handles {
            handle.abort();
        }
        for handle in handles {
            let _ = handle.await;
        }
    }
}

async fn spawn_tcp_listener(local_ip: Ipv4Addr, port: u16) -> Result<JoinHandle<()>> {
    let listener = TcpListener::bind((local_ip, port))
        .await
        .with_context(|| format!("bind TCP traffic listener {local_ip}:{port}"))?;
    Ok(tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((mut stream, _)) => {
                    tokio::spawn(async move {
                        let mut buffer = vec![0_u8; 64 * 1024];
                        while let Ok(bytes) = stream.read(&mut buffer).await {
                            if bytes == 0 {
                                break;
                            }
                        }
                    });
                }
                Err(error) => {
                    crate::logging::error(format!("TCP traffic accept failed on {port}: {error}"))
                }
            }
        }
    }))
}

async fn spawn_udp_listener(local_ip: Ipv4Addr, port: u16) -> Result<JoinHandle<()>> {
    let socket = UdpSocket::bind((local_ip, port))
        .await
        .with_context(|| format!("bind UDP traffic listener {local_ip}:{port}"))?;
    Ok(tokio::spawn(async move {
        let mut buffer = vec![0_u8; 65_535];
        loop {
            if let Err(error) = socket.recv_from(&mut buffer).await {
                crate::logging::error(format!("UDP traffic receive failed on {port}: {error}"));
                sleep(Duration::from_millis(100)).await;
            }
        }
    }))
}

enum ScheduledTransport {
    Udp {
        socket: Option<UdpSocket>,
        next_retry: Instant,
        retry_delay: Duration,
    },
    Tcp {
        stream: Option<TcpStream>,
        connect_task: Option<JoinHandle<Result<TcpStream>>>,
        next_retry: Instant,
        retry_delay: Duration,
    },
}

struct ScheduledFlow {
    plan: FlowPlan,
    transport: ScheduledTransport,
    payload: Vec<u8>,
    payload_bytes: usize,
    tokens: f64,
    previous: Instant,
    active: bool,
}

impl ScheduledFlow {
    fn new(plan: FlowPlan, now: Instant) -> Self {
        let payload_bytes = plan.payload_bytes.max(1);
        let transport = match plan.protocol {
            Transport::Udp => ScheduledTransport::Udp {
                socket: None,
                next_retry: now,
                retry_delay: MIN_RECONNECT,
            },
            Transport::Tcp => ScheduledTransport::Tcp {
                stream: None,
                connect_task: None,
                next_retry: now,
                retry_delay: MIN_RECONNECT,
            },
        };
        let buffer_bytes = match plan.protocol {
            Transport::Tcp => MAX_TCP_WRITE_BYTES,
            Transport::Udp => payload_bytes,
        };
        Self {
            payload: vec![0_u8; buffer_bytes],
            payload_bytes,
            plan,
            transport,
            tokens: 0.0,
            previous: now,
            active: false,
        }
    }
}

async fn traffic_scheduler(
    local_ip: Ipv4Addr,
    plans: Vec<FlowPlan>,
    status: Arc<Mutex<AgentStatus>>,
) {
    let now = Instant::now();
    let mut flows = plans
        .into_iter()
        .map(|plan| ScheduledFlow::new(plan, now))
        .collect::<Vec<_>>();
    let mut cadence = interval_at(
        now + scheduler_start_delay(local_ip, SystemTime::now()),
        TRAFFIC_SCHEDULER_TICK,
    );
    cadence.set_missed_tick_behavior(MissedTickBehavior::Skip);

    loop {
        cadence.tick().await;
        let now = Instant::now();
        let mut activity = SendActivity::default();
        for flow in &mut flows {
            activity += service_flow(local_ip, flow, now, &status).await;
        }
        record_scheduler_tick(&status, activity).await;
    }
}

#[derive(Default)]
struct SendActivity {
    packets: u64,
    bytes: u64,
}

impl std::ops::AddAssign for SendActivity {
    fn add_assign(&mut self, other: Self) {
        self.packets += other.packets;
        self.bytes += other.bytes;
    }
}

async fn service_flow(
    local_ip: Ipv4Addr,
    flow: &mut ScheduledFlow,
    now: Instant,
    status: &Arc<Mutex<AgentStatus>>,
) -> SendActivity {
    let mut activity = SendActivity::default();
    flow.tokens = replenish_tokens(
        flow.tokens,
        flow.plan.bandwidth_bps as f64 / 8.0,
        now - flow.previous,
        flow.payload_bytes,
    );
    flow.previous = now;

    match &mut flow.transport {
        ScheduledTransport::Udp {
            socket,
            next_retry,
            retry_delay,
        } => {
            if socket.is_none() && now >= *next_retry {
                match create_udp_sender(local_ip, &flow.plan).await {
                    Ok(created) => {
                        *socket = Some(created);
                        *retry_delay = MIN_RECONNECT;
                        set_flow_active(status, &mut flow.active, true).await;
                    }
                    Err(error) => {
                        record_error(
                            status,
                            format!("UDP {} setup failed: {error:#}", flow.plan.flow_id),
                        )
                        .await;
                        *next_retry = now + reconnect_delay_for(&flow.plan.flow_id, *retry_delay);
                        *retry_delay = (*retry_delay * 2).min(MAX_RECONNECT);
                    }
                }
            }
            let Some(socket) = socket else {
                return activity;
            };
            while flow.tokens >= flow.payload_bytes as f64 {
                match socket.try_send(&flow.payload[..flow.payload_bytes]) {
                    Ok(bytes) => {
                        set_flow_active(status, &mut flow.active, true).await;
                        flow.tokens -= bytes as f64;
                        activity.packets += 1;
                        activity.bytes += bytes as u64;
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                    Err(error) => {
                        set_flow_active(status, &mut flow.active, false).await;
                        record_error(
                            status,
                            format!("UDP {} send failed: {error}", flow.plan.flow_id),
                        )
                        .await;
                        break;
                    }
                }
            }
        }
        ScheduledTransport::Tcp {
            stream,
            connect_task,
            next_retry,
            retry_delay,
        } => {
            if connect_task.as_ref().is_some_and(JoinHandle::is_finished) {
                let completed = connect_task.take().expect("finished task");
                match completed.await {
                    Ok(Ok(connected)) => {
                        *stream = Some(connected);
                        *retry_delay = MIN_RECONNECT;
                        set_flow_active(status, &mut flow.active, true).await;
                    }
                    Ok(Err(error)) => {
                        record_error(
                            status,
                            format!("TCP {} connect failed: {error:#}", flow.plan.flow_id),
                        )
                        .await;
                        record_reconnect(status).await;
                        *next_retry = now + reconnect_delay_for(&flow.plan.flow_id, *retry_delay);
                        *retry_delay = (*retry_delay * 2).min(MAX_RECONNECT);
                    }
                    Err(error) => {
                        record_error(
                            status,
                            format!("TCP {} connect task failed: {error}", flow.plan.flow_id),
                        )
                        .await;
                        record_reconnect(status).await;
                        *next_retry = now + reconnect_delay_for(&flow.plan.flow_id, *retry_delay);
                        *retry_delay = (*retry_delay * 2).min(MAX_RECONNECT);
                    }
                }
            }
            if stream.is_none() && connect_task.is_none() && now >= *next_retry {
                *connect_task = Some(spawn_tcp_connect(local_ip, flow.plan.clone()));
            }
            let mut disconnect = None;
            if let Some(active_stream) = stream.as_mut() {
                while flow.tokens >= flow.payload_bytes as f64 {
                    let logical_payloads = (flow.tokens / flow.payload_bytes as f64) as usize;
                    let write_bytes = logical_payloads
                        .saturating_mul(flow.payload_bytes)
                        .min(MAX_TCP_WRITE_BYTES);
                    match active_stream.try_write(&flow.payload[..write_bytes]) {
                        Ok(0) => {
                            disconnect = Some(None);
                            break;
                        }
                        Ok(bytes) => {
                            flow.tokens -= bytes as f64;
                            activity.packets += bytes.div_ceil(flow.payload_bytes) as u64;
                            activity.bytes += bytes as u64;
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                        Err(error) => {
                            disconnect = Some(Some(error));
                            break;
                        }
                    }
                }
            }
            if let Some(error) = disconnect {
                *stream = None;
                set_flow_active(status, &mut flow.active, false).await;
                if let Some(error) = error {
                    record_error(
                        status,
                        format!("TCP {} send failed: {error}", flow.plan.flow_id),
                    )
                    .await;
                }
                record_reconnect(status).await;
                *next_retry = now + reconnect_delay_for(&flow.plan.flow_id, *retry_delay);
                *retry_delay = (*retry_delay * 2).min(MAX_RECONNECT);
            }
        }
    }
    activity
}

async fn create_udp_sender(local_ip: Ipv4Addr, flow: &FlowPlan) -> Result<UdpSocket> {
    let socket = UdpSocket::bind((local_ip, 0))
        .await
        .with_context(|| format!("bind UDP flow {}", flow.flow_id))?;
    socket
        .connect((flow.dst_ip, flow.dst_port))
        .await
        .with_context(|| format!("connect UDP flow {}", flow.flow_id))?;
    Ok(socket)
}

fn spawn_tcp_connect(local_ip: Ipv4Addr, flow: FlowPlan) -> JoinHandle<Result<TcpStream>> {
    tokio::spawn(async move {
        let socket = TcpSocket::new_v4().context("create TCP socket")?;
        socket
            .bind(SocketAddr::V4(SocketAddrV4::new(local_ip, 0)))
            .context("bind TCP socket")?;
        if flow.tcp_mss > 0 {
            set_tcp_mss(socket.as_raw_fd(), flow.tcp_mss).context("set TCP_MAXSEG")?;
        }
        let destination = SocketAddr::V4(SocketAddrV4::new(flow.dst_ip, flow.dst_port));
        timeout(CONNECT_TIMEOUT, socket.connect(destination))
            .await
            .context("TCP connect timed out")?
            .context("connect TCP flow")
    })
}

fn replenish_tokens(
    current: f64,
    bytes_per_second: f64,
    elapsed: Duration,
    payload_bytes: usize,
) -> f64 {
    let burst =
        (bytes_per_second * TRAFFIC_SCHEDULER_TICK.as_secs_f64() * 1.25).max(payload_bytes as f64);
    (current + bytes_per_second * elapsed.as_secs_f64()).min(burst)
}

fn scheduler_start_delay(local_ip: Ipv4Addr, now: SystemTime) -> Duration {
    let period_ns = TRAFFIC_SCHEDULER_TICK.as_nanos() as u64;
    let digest = Sha256::digest(local_ip.octets());
    let slot_ns = u64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix")) % period_ns;
    let current_ns = now
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
        % period_ns;
    Duration::from_nanos((slot_ns + period_ns - current_ns) % period_ns)
}

fn reconnect_delay_for(flow_id: &str, base: Duration) -> Duration {
    let digest = Sha256::digest(flow_id.as_bytes());
    let bucket = u16::from_be_bytes([digest[0], digest[1]]) as f64 / u16::MAX as f64;
    base.mul_f64(0.9 + bucket * 0.2).min(MAX_RECONNECT)
}

fn set_tcp_mss(fd: i32, value: u32) -> io::Result<()> {
    let value = value as libc::c_int;
    // SAFETY: fd is a live TCP socket and value is correctly sized.
    let result = unsafe {
        libc::setsockopt(
            fd,
            libc::IPPROTO_TCP,
            libc::TCP_MAXSEG,
            std::ptr::addr_of!(value).cast(),
            std::mem::size_of_val(&value) as libc::socklen_t,
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

async fn set_flow_active(status: &Arc<Mutex<AgentStatus>>, active: &mut bool, value: bool) {
    if *active == value {
        return;
    }
    *active = value;
    let mut status = status.lock().await;
    if value {
        status.active_flows += 1;
    } else {
        status.active_flows = status.active_flows.saturating_sub(1);
    }
    status.ready = status.active_listeners == status.expected_listeners
        && (!status.enabled || status.active_flows == status.expected_flows);
    if status.ready {
        status.last_error = None;
    }
}

async fn record_scheduler_tick(status: &Arc<Mutex<AgentStatus>>, activity: SendActivity) {
    let mut status = status.lock().await;
    status.packets_sent += activity.packets;
    status.bytes_sent += activity.bytes;
    status.refresh_heartbeat();
}

async fn record_reconnect(status: &Arc<Mutex<AgentStatus>>) {
    status.lock().await.reconnects += 1;
}

async fn record_error(status: &Arc<Mutex<AgentStatus>>, error: String) {
    status.lock().await.last_error = Some(error);
}

fn plan_digest(plan: &ClientTrafficPlan) -> Result<String> {
    let value = serde_json::to_value(plan)?;
    let canonical = canonical_json(&value);
    let digest = Sha256::digest(serde_json::to_vec(&canonical)?);
    Ok(format!("{digest:x}"))
}

fn canonical_json(value: &Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .iter()
                .map(|(key, value)| (key.clone(), canonical_json(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(canonical_json).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_is_stable_across_round_trip() {
        let plan = ClientTrafficPlan {
            listeners: vec![ListenerPlan {
                protocol: Transport::Udp,
                port: 5201,
            }],
            flows: vec![FlowPlan {
                flow_id: "flow-1".into(),
                protocol: Transport::Udp,
                dst_ip: "192.0.2.1".parse().unwrap(),
                dst_port: 5201,
                bandwidth_bps: 1_000_000,
                payload_bytes: 1400,
                tcp_mss: 0,
            }],
        };
        let first = plan_digest(&plan).unwrap();
        let decoded: ClientTrafficPlan =
            serde_json::from_slice(&serde_json::to_vec(&plan).unwrap()).unwrap();
        assert_eq!(first, plan_digest(&decoded).unwrap());
    }

    #[test]
    fn pacing_uses_elapsed_time_and_allows_one_low_rate_payload() {
        assert_eq!(
            replenish_tokens(0.0, 10_000.0, Duration::from_millis(50), 1400),
            500.0
        );
        assert_eq!(
            replenish_tokens(500.0, 10_000.0, Duration::from_millis(100), 1400),
            1400.0
        );
        assert_eq!(
            replenish_tokens(1400.0, 10_000.0, Duration::from_secs(1), 1400),
            1400.0
        );
        assert_eq!(
            replenish_tokens(0.0, 1_000_000.0, TRAFFIC_SCHEDULER_TICK, 1400),
            100_000.0
        );
    }

    #[test]
    fn scheduler_phase_is_deterministic_and_spreads_clients() {
        let now = UNIX_EPOCH + Duration::from_secs(10);
        let first = scheduler_start_delay("192.0.2.1".parse().unwrap(), now);
        assert_eq!(
            first,
            scheduler_start_delay("192.0.2.1".parse().unwrap(), now)
        );
        assert!(first < TRAFFIC_SCHEDULER_TICK);
        assert_ne!(
            first,
            scheduler_start_delay("192.0.2.2".parse().unwrap(), now)
        );
    }

    #[tokio::test]
    async fn recovered_flow_clears_transient_error_when_all_flows_are_ready() {
        let status = Arc::new(Mutex::new(AgentStatus {
            enabled: true,
            expected_flows: 1,
            active_listeners: 1,
            expected_listeners: 1,
            last_error: Some("transient path failure".into()),
            ..AgentStatus::default()
        }));
        let mut active = false;
        set_flow_active(&status, &mut active, true).await;
        let current = status.lock().await;
        assert!(current.ready);
        assert_eq!(current.active_flows, 1);
        assert_eq!(current.last_error, None);
    }

    #[tokio::test]
    async fn scheduler_tick_owns_the_traffic_heartbeat() {
        let status = Arc::new(Mutex::new(AgentStatus::default()));
        record_scheduler_tick(&status, SendActivity::default()).await;
        assert!(status.lock().await.heartbeat_unix_ns > 0);
    }

    #[tokio::test]
    async fn reloading_a_plan_waits_for_listener_sockets_to_close() {
        let udp = std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let udp_port = udp.local_addr().unwrap().port();
        drop(udp);
        let tcp = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let tcp_port = tcp.local_addr().unwrap().port();
        drop(tcp);

        let plan = ClientTrafficPlan {
            listeners: vec![
                ListenerPlan {
                    protocol: Transport::Udp,
                    port: udp_port,
                },
                ListenerPlan {
                    protocol: Transport::Tcp,
                    port: tcp_port,
                },
            ],
            flows: Vec::new(),
        };
        let digest = plan_digest(&plan).unwrap();
        let status = Arc::new(Mutex::new(AgentStatus::default()));
        let mut runtime = TrafficRuntime {
            local_ip: Ipv4Addr::LOCALHOST,
            status,
            plan: None,
            listener_tasks: Vec::new(),
            sender_task: None,
        };

        runtime
            .load(LoadPayload {
                generation: 1,
                plan_digest: digest.clone(),
                plan: plan.clone(),
            })
            .await
            .unwrap();
        runtime
            .load(LoadPayload {
                generation: 2,
                plan_digest: digest,
                plan,
            })
            .await
            .unwrap();
        assert_eq!(runtime.listener_tasks.len(), 2);
        runtime.stop_listeners().await;
    }

    #[tokio::test]
    async fn partial_listener_bind_failure_releases_earlier_sockets() {
        let udp = std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let udp_port = udp.local_addr().unwrap().port();
        drop(udp);
        let occupied_tcp = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let tcp_port = occupied_tcp.local_addr().unwrap().port();
        let plan = ClientTrafficPlan {
            listeners: vec![
                ListenerPlan {
                    protocol: Transport::Udp,
                    port: udp_port,
                },
                ListenerPlan {
                    protocol: Transport::Tcp,
                    port: tcp_port,
                },
            ],
            flows: Vec::new(),
        };
        let status = Arc::new(Mutex::new(AgentStatus::default()));
        let mut runtime = TrafficRuntime {
            local_ip: Ipv4Addr::LOCALHOST,
            status: Arc::clone(&status),
            plan: None,
            listener_tasks: Vec::new(),
            sender_task: None,
        };
        let error = runtime
            .load(LoadPayload {
                generation: 1,
                plan_digest: plan_digest(&plan).unwrap(),
                plan,
            })
            .await
            .unwrap_err();

        assert!(format!("{error:#}").contains("bind TCP traffic listener"));
        assert!(runtime.listener_tasks.is_empty());
        assert!(runtime.plan.is_none());
        assert!(!status.lock().await.ready);
        std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, udp_port)).unwrap();
    }

    #[test]
    fn reconnect_jitter_is_deterministic_and_bounded() {
        let first = reconnect_delay_for("flow-1", Duration::from_secs(1));
        assert_eq!(first, reconnect_delay_for("flow-1", Duration::from_secs(1)));
        assert!((Duration::from_millis(900)..=Duration::from_millis(1100)).contains(&first));
        assert!(reconnect_delay_for("flow-1", Duration::from_secs(5)) <= MAX_RECONNECT);
    }
}

use std::collections::HashMap;
use std::io::{self, IoSliceMut};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::os::fd::AsRawFd;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use blake2::Blake2sVar;
use blake2::digest::{Update, VariableOutput};
use nix::errno::Errno;
use nix::sys::socket::{ControlMessageOwned, MsgFlags, SockaddrIn, recvmsg};
use nix::sys::time::TimeValLike;
use socket2::{Domain, Protocol, SockAddr, Socket, Type};
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;
use tokio::sync::{Mutex, mpsc};
use tokio::time::{Instant, MissedTickBehavior, interval_at, sleep, sleep_until, timeout};

use crate::config::{Client, ClientAgentConfig, PINGMESH_CONTROL_PORT};
use crate::control::{self, AgentStatus, ResponseEnvelope};

const UDP_DESTINATION_PORT: u16 = 33434;
const RTT_SOURCE_PORT_BASE: u16 = 33000;
const PROBE_MAGIC: u32 = 0x4e4f_4250;
const PROBE_VERSION: u8 = 2;
const HEADER_SIZE: usize = 24;
const RTT_PAYLOAD_BYTES: usize = 64;
const RECEIVE_BUFFER_BYTES: usize = 256 * 1024;
const METRICS_QUEUE_CAPACITY: usize = 4096;
const METRICS_BATCH_SIZE: usize = 50;
const METRICS_BATCH_TIMEOUT: Duration = Duration::from_secs(2);
const INGEST_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const PROBE_CYCLE_SLACK: Duration = Duration::from_millis(50);
const REPLY_DRAIN_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Clone, Debug)]
struct Target {
    client: Client,
    path_type: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProbeKind {
    Rtt,
    Df,
}

#[derive(Clone, Copy, Debug)]
struct Pending {
    kind: ProbeKind,
    target_index: usize,
    sent_ns: u64,
}

#[derive(Clone, Debug, Default)]
struct ProbeStats {
    sent: u64,
    received: u64,
    rtts_ms: Vec<f64>,
    mtu_drops: u64,
}

#[derive(Clone, Debug)]
struct ProbeResult {
    rtt_min: f64,
    rtt_avg: f64,
    rtt_max: f64,
    rtt_p90: f64,
    rtt_p99: f64,
    packets_sent: u64,
    packets_lost: u64,
    packet_loss: f64,
    rtt_ports_active: usize,
    rtt_ports_total: usize,
    probe_cycle: u64,
    destination_batch_index: usize,
    port_batch_index: usize,
    df_packets_sent: u64,
    df_packets_lost: u64,
    df_mtu_drops: u64,
}

struct ProbeSocket {
    socket: AsyncFd<Socket>,
}

#[derive(Clone)]
struct IngestConfig {
    host: String,
    port: u16,
    path: String,
    token: String,
}

pub async fn run(path: &Path) -> Result<()> {
    let config = ClientAgentConfig::load(path)?;
    let local = config.local_client()?;
    let status = Arc::new(Mutex::new(AgentStatus::default()));
    let responder = create_udp_socket(local.data_ip, UDP_DESTINATION_PORT, true, true)
        .context("create Pingmesh responder")?;
    let probe_sockets =
        create_probe_sockets(local.data_ip, config.pingmesh_policy.rtt_port_pool_size)?;
    let ingest = IngestConfig::from_environment()?;
    let (metrics_tx, metrics_rx) = mpsc::channel(METRICS_QUEUE_CAPACITY);
    {
        let mut current = status.lock().await;
        current.ready = true;
        current.active_listeners = 1;
        current.expected_listeners = 1;
    }

    let control_status = Arc::clone(&status);
    let control_address = SocketAddr::V4(SocketAddrV4::new(
        local.management_ip,
        PINGMESH_CONTROL_PORT,
    ));
    let control_task = control::serve(control_address, move |request| {
        let status = Arc::clone(&control_status);
        async move {
            let snapshot = status.lock().await.clone();
            if request.op == "status" {
                ResponseEnvelope::ok(snapshot)
            } else {
                ResponseEnvelope::error(snapshot, format!("unsupported operation {}", request.op))
            }
        }
    });

    let responder_task = responder_loop(responder, Arc::clone(&status));
    let writer_task = metrics_writer(metrics_rx, ingest, Arc::clone(&status));
    let probe_task = probe_loop(
        config,
        local,
        probe_sockets,
        metrics_tx,
        Arc::clone(&status),
    );

    tokio::select! {
        result = control_task => result.context("Pingmesh control server"),
        result = responder_task => result.context("Pingmesh responder"),
        result = writer_task => result.context("Pingmesh metrics writer"),
        result = probe_task => result.context("Pingmesh probe loop"),
        result = tokio::signal::ctrl_c() => {
            result.context("wait for shutdown signal")?;
            Ok(())
        },
    }
}

fn create_probe_sockets(data_ip: Ipv4Addr, count: usize) -> Result<Vec<ProbeSocket>> {
    let mut sockets = Vec::with_capacity(count);
    for offset in 0..count {
        let source_port = RTT_SOURCE_PORT_BASE
            .checked_add(u16::try_from(offset).context("source-port pool is too large")?)
            .context("source-port pool overflow")?;
        let socket = create_udp_socket(data_ip, source_port, true, false)
            .with_context(|| format!("bind Pingmesh source port {source_port}"))?;
        set_df(socket.get_ref())?;
        sockets.push(ProbeSocket { socket });
    }
    Ok(sockets)
}

fn create_udp_socket(
    address: Ipv4Addr,
    port: u16,
    timestamp: bool,
    tune_receive_buffer: bool,
) -> Result<AsyncFd<Socket>> {
    let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    socket.set_nonblocking(true)?;
    if tune_receive_buffer {
        socket.set_recv_buffer_size(RECEIVE_BUFFER_BYTES)?;
    }
    if timestamp {
        enable_receive_timestamp(&socket)?;
    }
    socket.bind(&SockAddr::from(SocketAddrV4::new(address, port)))?;
    AsyncFd::new(socket).context("register UDP socket")
}

fn enable_receive_timestamp(socket: &Socket) -> Result<()> {
    let enabled: libc::c_int = 1;
    // SAFETY: the file descriptor is valid and the option points to a
    // correctly sized integer for the duration of the call.
    let result = unsafe {
        libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_TIMESTAMPNS,
            std::ptr::addr_of!(enabled).cast(),
            std::mem::size_of_val(&enabled) as libc::socklen_t,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error()).context("SO_TIMESTAMPNS is required");
    }
    Ok(())
}

fn set_df(socket: &Socket) -> Result<()> {
    let value: libc::c_int = libc::IP_PMTUDISC_DO;
    // SAFETY: the file descriptor and option pointer are valid.
    let result = unsafe {
        libc::setsockopt(
            socket.as_raw_fd(),
            libc::IPPROTO_IP,
            libc::IP_MTU_DISCOVER,
            std::ptr::addr_of!(value).cast(),
            std::mem::size_of_val(&value) as libc::socklen_t,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error()).context("set IP_MTU_DISCOVER");
    }
    Ok(())
}

async fn responder_loop(socket: AsyncFd<Socket>, status: Arc<Mutex<AgentStatus>>) -> Result<()> {
    let mut buffer = vec![0_u8; 65_535];
    loop {
        let mut guard = socket.readable().await?;
        loop {
            let received = guard.try_io(|inner| recv_timestamped(inner.get_ref(), &mut buffer));
            match received {
                Ok(Ok(packet)) => {
                    let Some(received_ns) = packet.received_ns else {
                        mark_protocol_error(&status, "Pingmesh request lacked kernel RX timestamp")
                            .await;
                        continue;
                    };
                    let bytes = packet.bytes;
                    if bytes < HEADER_SIZE {
                        mark_protocol_error(&status, "short Pingmesh request").await;
                        continue;
                    }
                    let Some((sequence, _)) = decode_header(&buffer[..bytes]) else {
                        mark_protocol_error(&status, "invalid Pingmesh request header").await;
                        continue;
                    };
                    let processing_ns = realtime_ns().saturating_sub(received_ns);
                    encode_header(&mut buffer[..bytes], sequence, processing_ns)?;
                    if let Err(error) = socket.get_ref().send_to(&buffer[..bytes], &packet.address)
                        && error.kind() != io::ErrorKind::WouldBlock
                    {
                        set_error(&status, format!("Pingmesh echo failed: {error}")).await;
                    }
                }
                Err(_) => break,
                Ok(Err(error)) => return Err(error).context("receive Pingmesh request"),
            }
        }
    }
}

async fn probe_loop(
    config: ClientAgentConfig,
    local: Client,
    sockets: Vec<ProbeSocket>,
    metrics_tx: mpsc::Sender<String>,
    status: Arc<Mutex<AgentStatus>>,
) -> Result<()> {
    let targets = build_targets(&config, &local);
    let policy = config.pingmesh_policy.clone();
    let destination_batch_size = policy
        .destination_batch_size
        .unwrap_or(targets.len())
        .clamp(1, targets.len());
    let destination_phase = config
        .clients
        .iter()
        .position(|client| client.name == local.name)
        .unwrap_or(0);
    let port_phase = stable_host_seed(&local.name) as usize % policy.port_batch_count;
    let startup_jitter =
        (stable_host_seed(&local.name) % 10_000) as f64 / 10_000.0 * policy.cycle_interval_seconds;
    sleep(Duration::from_secs_f64(startup_jitter)).await;

    let cycle_duration = Duration::from_secs_f64(policy.cycle_interval_seconds);
    let probe_duration = probe_work_duration(cycle_duration);
    let mut cadence = interval_at(Instant::now(), cycle_duration);
    cadence.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut cycle = 0_u64;
    let mut sequence = 0_u64;

    loop {
        cadence.tick().await;
        let cycle_started = Instant::now();
        let (destination_batch, port_batch) = probe_batch_indices(
            cycle,
            policy.destination_batch_count,
            policy.port_batch_count,
            port_phase,
        );
        let active_targets = rotating_destination_batch(
            &targets,
            destination_batch_size,
            destination_batch,
            destination_phase,
        );
        let port_start = port_batch * policy.rtt_ports_per_cycle;
        let port_end = (port_start + policy.rtt_ports_per_cycle).min(sockets.len());
        let active_sockets = &sockets[port_start..port_end];
        let results = run_probe_cycle(
            &active_targets,
            active_sockets,
            policy.df_payload_size,
            probe_duration,
            &mut sequence,
        )
        .await?;

        for (target, mut result) in active_targets.iter().zip(results) {
            result.probe_cycle = cycle;
            result.destination_batch_index = destination_batch;
            result.port_batch_index = port_batch;
            result.rtt_ports_total = sockets.len();
            let timestamp = realtime_ns();
            for line in metric_lines(&config.topology_id, &local, target, &result, timestamp) {
                if metrics_tx.try_send(line).is_err() {
                    let mut current = status.lock().await;
                    current.ready = false;
                    current.last_error = Some("Pingmesh metrics queue is full".to_string());
                    bail!("Pingmesh metrics queue is full");
                }
            }
        }
        let cycle_duration_ms = cycle_started.elapsed().as_secs_f64() * 1_000.0;
        {
            let mut current = status.lock().await;
            current.completed_cycles = current.completed_cycles.saturating_add(1);
            current.last_cycle_duration_ms = cycle_duration_ms;
            current.max_cycle_duration_ms = current.max_cycle_duration_ms.max(cycle_duration_ms);
            current.refresh_heartbeat();
        }
        cycle = cycle.wrapping_add(1);
    }
}

fn probe_work_duration(cycle_duration: Duration) -> Duration {
    cycle_duration.saturating_sub(PROBE_CYCLE_SLACK.min(cycle_duration.div_f64(10.0)))
}

async fn run_probe_cycle(
    targets: &[Target],
    sockets: &[ProbeSocket],
    df_payload_size: usize,
    duration: Duration,
    sequence: &mut u64,
) -> Result<Vec<ProbeResult>> {
    let mut rtt_stats = vec![ProbeStats::default(); targets.len()];
    let mut df_stats = vec![ProbeStats::default(); targets.len()];
    drain_sockets(sockets)?;
    let mut pending: HashMap<u64, Pending> = HashMap::new();
    let mut send_queue = Vec::new();
    for target_index in 0..targets.len() {
        for socket_index in 0..sockets.len() {
            send_queue.push((ProbeKind::Rtt, target_index, socket_index));
        }
        if !sockets.is_empty() {
            send_queue.push((ProbeKind::Df, target_index, target_index % sockets.len()));
        }
    }

    let started = Instant::now();
    let deadline = started + duration;
    let send_window = duration.div_f64(2.0);
    let spacing = if send_queue.len() > 1 {
        send_window.div_f64((send_queue.len() - 1) as f64)
    } else {
        Duration::ZERO
    };

    for (index, (kind, target_index, socket_index)) in send_queue.into_iter().enumerate() {
        sleep_until(started + spacing.mul_f64(index as f64)).await;
        let target = &targets[target_index];
        let probe_socket = &sockets[socket_index];
        let payload_size = match kind {
            ProbeKind::Rtt => RTT_PAYLOAD_BYTES,
            ProbeKind::Df => df_payload_size,
        }
        .max(HEADER_SIZE);
        let mut payload = vec![0_u8; payload_size];
        encode_header(&mut payload, *sequence, 0)?;
        let destination = SockAddr::from(SocketAddrV4::new(
            target.client.data_ip,
            UDP_DESTINATION_PORT,
        ));
        let stats = match kind {
            ProbeKind::Rtt => &mut rtt_stats[target_index],
            ProbeKind::Df => &mut df_stats[target_index],
        };
        match probe_socket
            .socket
            .get_ref()
            .send_to(&payload, &destination)
        {
            Ok(_) => {
                // Timestamp after the non-blocking syscall. If the process is
                // descheduled immediately before sendto(), a pre-call
                // timestamp would turn host scheduling delay into network RTT.
                let sent_ns = realtime_ns();
                stats.sent += 1;
                pending.insert(
                    *sequence,
                    Pending {
                        kind,
                        target_index,
                        sent_ns,
                    },
                );
            }
            Err(error) if error.raw_os_error() == Some(libc::EMSGSIZE) => {
                stats.sent += 1;
                stats.mtu_drops += 1;
            }
            Err(error) if is_transient_local_send_error(&error) => {}
            Err(error) if is_path_send_error(&error) => {
                // ACL, routing, and peer reachability failures are data-plane
                // observations. The missing reply records the loss; they must
                // never terminate the long-lived probe process.
                stats.sent += 1;
            }
            Err(error) => return Err(error).context("send Pingmesh probe"),
        }
        *sequence = sequence.wrapping_add(1);
    }

    drain_replies(sockets, &mut pending, &mut rtt_stats, &mut df_stats)?;
    while Instant::now() < deadline && !pending.is_empty() {
        drain_replies(sockets, &mut pending, &mut rtt_stats, &mut df_stats)?;
        sleep(REPLY_DRAIN_INTERVAL).await;
    }
    drain_replies(sockets, &mut pending, &mut rtt_stats, &mut df_stats)?;

    Ok((0..targets.len())
        .map(|index| build_result(&rtt_stats[index], &df_stats[index], sockets.len()))
        .collect())
}

fn drain_sockets(sockets: &[ProbeSocket]) -> Result<()> {
    let mut buffer = vec![0_u8; 65_535];
    for socket in sockets {
        loop {
            match recv_timestamped(socket.socket.get_ref(), &mut buffer) {
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) => return Err(error).context("drain stale Pingmesh reply"),
            }
        }
    }
    Ok(())
}

fn drain_replies(
    sockets: &[ProbeSocket],
    pending: &mut HashMap<u64, Pending>,
    rtt_stats: &mut [ProbeStats],
    df_stats: &mut [ProbeStats],
) -> Result<()> {
    let mut buffer = vec![0_u8; 65_535];
    for socket in sockets {
        loop {
            let packet = match recv_timestamped(socket.socket.get_ref(), &mut buffer) {
                Ok(packet) => packet,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) => return Err(error).context("receive Pingmesh reply"),
            };
            let Some(received_ns) = packet.received_ns else {
                continue;
            };
            let bytes = packet.bytes;
            let Some((sequence, responder_processing_ns)) = decode_header(&buffer[..bytes]) else {
                continue;
            };
            let Some(item) = pending.remove(&sequence) else {
                continue;
            };
            let adjusted_ns = adjusted_rtt_ns(item.sent_ns, received_ns, responder_processing_ns);
            let stats = match item.kind {
                ProbeKind::Rtt => &mut rtt_stats[item.target_index],
                ProbeKind::Df => &mut df_stats[item.target_index],
            };
            stats.received += 1;
            stats.rtts_ms.push(adjusted_ns as f64 / 1_000_000.0);
        }
    }
    Ok(())
}

#[derive(Debug)]
struct ReceivedPacket {
    bytes: usize,
    address: SockAddr,
    received_ns: Option<u64>,
}

fn recv_timestamped(socket: &Socket, buffer: &mut [u8]) -> io::Result<ReceivedPacket> {
    let mut iov = [IoSliceMut::new(buffer)];
    let mut cmsgspace = nix::cmsg_space!(libc::timespec);
    let message = match recvmsg::<SockaddrIn>(
        socket.as_raw_fd(),
        &mut iov,
        Some(&mut cmsgspace),
        MsgFlags::MSG_DONTWAIT,
    ) {
        Ok(message) => message,
        Err(Errno::EAGAIN) => {
            return Err(io::Error::from(io::ErrorKind::WouldBlock));
        }
        Err(error) => return Err(io::Error::from_raw_os_error(error as i32)),
    };
    let bytes = message.bytes;
    let address = message
        .address
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing UDP source address"))?;
    let mut timestamp = None;
    for control in message
        .cmsgs()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
    {
        if let ControlMessageOwned::ScmTimestampns(value) = control {
            timestamp = Some(u64::try_from(value.num_nanoseconds()).map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "negative RX timestamp")
            })?);
        }
    }
    let standard = SocketAddrV4::new(address.ip(), address.port());
    Ok(ReceivedPacket {
        bytes,
        address: SockAddr::from(standard),
        received_ns: timestamp,
    })
}

fn is_transient_local_send_error(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::WouldBlock || error.raw_os_error() == Some(libc::ENOBUFS)
}

fn is_path_send_error(error: &io::Error) -> bool {
    matches!(
        error.raw_os_error(),
        Some(
            libc::EPERM
                | libc::EACCES
                | libc::ENETUNREACH
                | libc::EHOSTUNREACH
                | libc::ECONNREFUSED
                | libc::EADDRNOTAVAIL
        )
    )
}

fn encode_header(buffer: &mut [u8], sequence: u64, processing_ns: u64) -> Result<()> {
    if buffer.len() < HEADER_SIZE {
        bail!("Pingmesh payload is smaller than the protocol header");
    }
    buffer[0..4].copy_from_slice(&PROBE_MAGIC.to_be_bytes());
    buffer[4] = PROBE_VERSION;
    buffer[5] = 0;
    buffer[6..8].fill(0);
    buffer[8..16].copy_from_slice(&sequence.to_be_bytes());
    buffer[16..24].copy_from_slice(&processing_ns.to_be_bytes());
    Ok(())
}

fn decode_header(buffer: &[u8]) -> Option<(u64, u64)> {
    if buffer.len() < HEADER_SIZE
        || u32::from_be_bytes(buffer[0..4].try_into().ok()?) != PROBE_MAGIC
        || buffer[4] != PROBE_VERSION
    {
        return None;
    }
    Some((
        u64::from_be_bytes(buffer[8..16].try_into().ok()?),
        u64::from_be_bytes(buffer[16..24].try_into().ok()?),
    ))
}

fn adjusted_rtt_ns(sent_ns: u64, received_ns: u64, responder_processing_ns: u64) -> u64 {
    // The request can reach a same-host peer before sendto() returns and the
    // post-send userspace timestamp is sampled. Clamp that sub-syscall
    // uncertainty to zero: a decoded reply is received evidence and must
    // never become synthetic packet loss.
    received_ns
        .saturating_sub(sent_ns)
        .saturating_sub(responder_processing_ns)
}

fn realtime_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_nanos()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn build_targets(config: &ClientAgentConfig, local: &Client) -> Vec<Target> {
    let local_index = config
        .clients
        .iter()
        .position(|client| client.name == local.name)
        .expect("validated local client");
    (1..config.clients.len())
        .map(|offset| {
            let client = config.clients[(local_index + offset) % config.clients.len()].clone();
            let path_type = if client.rack == local.rack {
                "same_rack"
            } else {
                "cross_rack"
            };
            Target { client, path_type }
        })
        .collect()
}

fn rotating_destination_batch(
    targets: &[Target],
    batch_size: usize,
    batch_index: usize,
    phase_offset: usize,
) -> Vec<Target> {
    if targets.is_empty() {
        return Vec::new();
    }
    let size = batch_size.clamp(1, targets.len());
    let batch_count = targets.len().div_ceil(size);
    let phase = phase_offset % targets.len();
    let ordered = targets[phase..]
        .iter()
        .chain(targets[..phase].iter())
        .cloned()
        .collect::<Vec<_>>();
    ordered
        .into_iter()
        .skip(batch_index % batch_count)
        .step_by(batch_count)
        .collect()
}

fn probe_batch_indices(
    cycle: u64,
    destination_batch_count: usize,
    port_batch_count: usize,
    port_phase: usize,
) -> (usize, usize) {
    let destination_count = destination_batch_count.max(1);
    let port_count = port_batch_count.max(1);
    let epoch_cycle = cycle as usize % (destination_count * port_count);
    let destination_batch = epoch_cycle % destination_count;
    let port_round = epoch_cycle / destination_count;
    (destination_batch, (port_round + port_phase) % port_count)
}

fn stable_host_seed(hostname: &str) -> u64 {
    let mut hasher = Blake2sVar::new(8).expect("BLAKE2s supports an eight-byte digest");
    hasher.update(hostname.as_bytes());
    let mut digest = [0_u8; 8];
    hasher
        .finalize_variable(&mut digest)
        .expect("digest buffer has the configured size");
    u64::from_be_bytes(digest)
}

fn build_result(rtt: &ProbeStats, df: &ProbeStats, active_ports: usize) -> ProbeResult {
    ProbeResult {
        rtt_min: rtt.rtts_ms.iter().copied().reduce(f64::min).unwrap_or(0.0),
        rtt_avg: mean(&rtt.rtts_ms),
        rtt_max: rtt.rtts_ms.iter().copied().reduce(f64::max).unwrap_or(0.0),
        rtt_p90: percentile(&rtt.rtts_ms, 0.90),
        rtt_p99: percentile(&rtt.rtts_ms, 0.99),
        packets_sent: rtt.sent,
        packets_lost: rtt.sent.saturating_sub(rtt.received),
        packet_loss: percentage_lost(rtt),
        rtt_ports_active: active_ports,
        rtt_ports_total: active_ports,
        probe_cycle: 0,
        destination_batch_index: 0,
        port_batch_index: 0,
        df_packets_sent: df.sent,
        df_packets_lost: df.sent.saturating_sub(df.received),
        df_mtu_drops: df.mtu_drops,
    }
}

fn percentage_lost(stats: &ProbeStats) -> f64 {
    if stats.sent == 0 {
        100.0
    } else {
        stats.sent.saturating_sub(stats.received) as f64 / stats.sent as f64 * 100.0
    }
}

fn mean(values: &[f64]) -> f64 {
    if values.is_empty() {
        0.0
    } else {
        values.iter().sum::<f64>() / values.len() as f64
    }
}

fn percentile(values: &[f64], quantile: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut ordered = values.to_vec();
    ordered.sort_by(f64::total_cmp);
    let index = ((ordered.len() as f64 * quantile) as usize).min(ordered.len() - 1);
    ordered[index]
}

fn metric_lines(
    topology_id: &str,
    source: &Client,
    target: &Target,
    result: &ProbeResult,
    timestamp: u64,
) -> Vec<String> {
    let tags = format!(
        "src_ip={},dst_ip={},src_name={},dst_name={},src_leaf={},dst_leaf={},path_type={},topology_id={}",
        escape_tag(&source.data_ip.to_string()),
        escape_tag(&target.client.data_ip.to_string()),
        escape_tag(&source.name),
        escape_tag(&target.client.name),
        escape_tag(&source.leaf),
        escape_tag(&target.client.leaf),
        target.path_type,
        escape_tag(topology_id),
    );
    let fields = format!(
        "rtt_min={},rtt_avg={},rtt_max={},rtt_p90={},rtt_p99={},packets_sent={}i,packets_lost={}i,packet_loss={},rtt_ports_active={}i,rtt_ports_total={}i,probe_cycle={}i,destination_batch_index={}i,port_batch_index={}i,df_packets_sent={}i,df_packets_lost={}i,df_mtu_drops={}i",
        result.rtt_min,
        result.rtt_avg,
        result.rtt_max,
        result.rtt_p90,
        result.rtt_p99,
        result.packets_sent,
        result.packets_lost,
        result.packet_loss,
        result.rtt_ports_active,
        result.rtt_ports_total,
        result.probe_cycle,
        result.destination_batch_index,
        result.port_batch_index,
        result.df_packets_sent,
        result.df_packets_lost,
        result.df_mtu_drops,
    );
    vec![format!("pingmesh,{tags} {fields} {timestamp}")]
}

fn escape_tag(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace(' ', "\\ ")
        .replace(',', "\\,")
        .replace('=', "\\=")
}

impl IngestConfig {
    fn from_environment() -> Result<Self> {
        let raw_url = std::env::var("NETOPSBENCH_INFLUXDB_URL")
            .unwrap_or_else(|_| "http://telegraf:8186".to_string());
        let authority = raw_url
            .strip_prefix("http://")
            .context("Pingmesh ingest URL must use http://")?;
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) => (
                host.to_string(),
                port.parse().context("invalid ingest port")?,
            ),
            None => (authority.to_string(), 80),
        };
        let org =
            std::env::var("NETOPSBENCH_INFLUXDB_ORG").unwrap_or_else(|_| "netopsbench".into());
        let bucket =
            std::env::var("NETOPSBENCH_INFLUXDB_BUCKET").unwrap_or_else(|_| "netopsbench".into());
        let token = std::env::var("NETOPSBENCH_INFLUXDB_TOKEN").unwrap_or_default();
        Ok(Self {
            host,
            port,
            path: format!("/api/v2/write?org={org}&bucket={bucket}&precision=ns"),
            token,
        })
    }
}

async fn metrics_writer(
    mut receiver: mpsc::Receiver<String>,
    ingest: IngestConfig,
    status: Arc<Mutex<AgentStatus>>,
) -> Result<()> {
    let mut batch = Vec::with_capacity(METRICS_BATCH_SIZE);
    let mut deadline = Instant::now() + METRICS_BATCH_TIMEOUT;
    loop {
        tokio::select! {
            item = receiver.recv() => {
                let Some(line) = item else {
                    if !batch.is_empty() {
                        post_metrics(&ingest, &batch).await?;
                    }
                    return Ok(());
                };
                batch.push(line);
                if batch.len() >= METRICS_BATCH_SIZE {
                    write_batch(&ingest, &mut batch, &status).await?;
                    deadline = Instant::now() + METRICS_BATCH_TIMEOUT;
                }
            }
            _ = sleep_until(deadline) => {
                if !batch.is_empty() {
                    write_batch(&ingest, &mut batch, &status).await?;
                }
                deadline = Instant::now() + METRICS_BATCH_TIMEOUT;
            }
        }
    }
}

async fn write_batch(
    ingest: &IngestConfig,
    batch: &mut Vec<String>,
    status: &Arc<Mutex<AgentStatus>>,
) -> Result<()> {
    let mut delay = Duration::from_millis(500);
    loop {
        match post_metrics(ingest, batch).await {
            Ok(()) => {
                batch.clear();
                let mut current = status.lock().await;
                current.ready = true;
                current.last_error = None;
                return Ok(());
            }
            Err(error) => {
                let mut current = status.lock().await;
                current.ready = false;
                current.last_error = Some(format!("Pingmesh metrics write failed: {error:#}"));
            }
        }
        sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(5));
    }
}

async fn post_metrics(ingest: &IngestConfig, lines: &[String]) -> Result<()> {
    timeout(INGEST_REQUEST_TIMEOUT, post_metrics_inner(ingest, lines))
        .await
        .context("topology-local Telegraf request timed out")?
}

async fn post_metrics_inner(ingest: &IngestConfig, lines: &[String]) -> Result<()> {
    let body = lines.join("\n");
    let mut stream = TcpStream::connect((ingest.host.as_str(), ingest.port))
        .await
        .context("connect to topology-local Telegraf")?;
    let request = format!(
        "POST {} HTTP/1.1\r\nHost: {}:{}\r\nAuthorization: Token {}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        ingest.path,
        ingest.host,
        ingest.port,
        ingest.token,
        body.len(),
        body,
    );
    stream.write_all(request.as_bytes()).await?;
    stream.shutdown().await?;
    let mut first_line = String::new();
    BufReader::new(stream).read_line(&mut first_line).await?;
    if !first_line.contains(" 204 ") {
        bail!("Telegraf returned {}", first_line.trim());
    }
    Ok(())
}

async fn set_error(status: &Arc<Mutex<AgentStatus>>, error: String) {
    let mut current = status.lock().await;
    current.last_error = Some(error);
}

async fn mark_protocol_error(status: &Arc<Mutex<AgentStatus>>, error: &str) {
    let mut current = status.lock().await;
    current.protocol_errors += 1;
    current.last_error = Some(error.to_string());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn responder_scheduling_is_removed_from_adjusted_rtt() {
        let sent = 1_000_000_000;
        let network = 4_000_000;
        let responder_wait = 30_000_000;
        let received = sent + network + responder_wait;
        assert_eq!(adjusted_rtt_ns(sent, received, responder_wait), network);
    }

    #[test]
    fn network_latency_remains_in_adjusted_rtt() {
        let sent = 1_000_000_000;
        let network = 120_000_000;
        let responder_wait = 3_000_000;
        let received = sent + network + responder_wait;
        assert_eq!(adjusted_rtt_ns(sent, received, responder_wait), network);
    }

    #[test]
    fn post_send_timestamp_uncertainty_does_not_drop_a_received_reply() {
        let post_send = 1_000_000_100;
        let reply_received = 1_000_000_090;

        assert_eq!(adjusted_rtt_ns(post_send, reply_received, 30), 0);
    }

    #[test]
    fn protocol_header_is_versioned_and_round_trips() {
        let mut buffer = [0_u8; HEADER_SIZE];
        encode_header(&mut buffer, 42, 900).unwrap();
        assert_eq!(decode_header(&buffer), Some((42, 900)));
        buffer[4] = 1;
        assert_eq!(decode_header(&buffer), None);
    }

    #[test]
    fn host_seed_is_stable_for_the_same_client() {
        assert_eq!(stable_host_seed("client-17"), 13_881_939_690_399_091_852);
    }

    #[test]
    fn destination_and_port_rotation_is_deterministic() {
        let clients = (0..8)
            .map(|index| Client {
                name: format!("client{index}"),
                data_ip: Ipv4Addr::new(192, 0, 2, index + 1),
                management_ip: Ipv4Addr::new(172, 20, 0, index + 1),
                rack: format!("rack{}", index / 2),
                leaf: format!("leaf{}", index / 2),
            })
            .collect::<Vec<_>>();
        let targets = clients[1..]
            .iter()
            .cloned()
            .map(|client| Target {
                client,
                path_type: "cross_rack",
            })
            .collect::<Vec<_>>();
        let batches = (0..3)
            .map(|batch| {
                rotating_destination_batch(&targets, 3, batch, 2)
                    .into_iter()
                    .map(|target| target.client.name)
                    .collect::<Vec<_>>()
            })
            .collect::<Vec<_>>();

        assert_eq!(
            batches,
            vec![
                vec!["client3", "client6", "client2"],
                vec!["client4", "client7"],
                vec!["client5", "client1"],
            ]
        );
        assert_eq!(probe_batch_indices(0, 3, 4, 2), (0, 2));
        assert_eq!(probe_batch_indices(3, 3, 4, 2), (0, 3));
        assert_eq!(probe_batch_indices(12, 3, 4, 2), (0, 2));
    }

    #[test]
    fn probe_work_uses_the_topology_cycle_instead_of_a_fixed_cap() {
        assert_eq!(
            probe_work_duration(Duration::from_secs(1)),
            Duration::from_millis(950)
        );
        assert_eq!(
            probe_work_duration(Duration::from_secs(2)),
            Duration::from_millis(1950)
        );
    }

    #[test]
    fn missing_timestamp_marks_only_the_packet_invalid() {
        let receiver = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP)).unwrap();
        receiver
            .bind(&SockAddr::from(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)))
            .unwrap();
        let address = receiver.local_addr().unwrap().as_socket_ipv4().unwrap();
        let sender = std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        sender.send_to(b"probe", address).unwrap();
        let mut buffer = [0_u8; 64];
        let packet = (0..100)
            .find_map(|_| match recv_timestamped(&receiver, &mut buffer) {
                Ok(packet) => Some(packet),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => None,
                Err(error) => panic!("receive without timestamp support: {error}"),
            })
            .expect("loopback packet");

        assert_eq!(packet.bytes, 5);
        assert_eq!(packet.received_ns, None);
    }

    #[test]
    fn empty_nonblocking_socket_reports_would_block() {
        let receiver = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP)).unwrap();
        receiver.set_nonblocking(true).unwrap();
        receiver
            .bind(&SockAddr::from(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)))
            .unwrap();
        let mut buffer = [0_u8; 64];

        let error = recv_timestamped(&receiver, &mut buffer).unwrap_err();

        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn path_send_errors_are_loss_signals_not_process_failures() {
        for errno in [
            libc::EPERM,
            libc::EACCES,
            libc::ENETUNREACH,
            libc::EHOSTUNREACH,
            libc::ECONNREFUSED,
            libc::EADDRNOTAVAIL,
        ] {
            assert!(is_path_send_error(&io::Error::from_raw_os_error(errno)));
        }
        assert!(!is_path_send_error(&io::Error::from_raw_os_error(
            libc::EBADF
        )));
        assert!(is_transient_local_send_error(
            &io::Error::from_raw_os_error(libc::ENOBUFS)
        ));
    }

    #[test]
    fn line_protocol_snapshot_remains_compatible() {
        let source = Client {
            name: "client1".into(),
            data_ip: "192.0.2.1".parse().unwrap(),
            management_ip: "172.20.0.1".parse().unwrap(),
            rack: "rack 1".into(),
            leaf: "leaf1".into(),
        };
        let target = Target {
            client: Client {
                name: "client2".into(),
                data_ip: "192.0.2.2".parse().unwrap(),
                management_ip: "172.20.0.2".parse().unwrap(),
                rack: "rack2".into(),
                leaf: "leaf2".into(),
            },
            path_type: "cross_rack",
        };
        let result = ProbeResult {
            rtt_min: 1.0,
            rtt_avg: 2.0,
            rtt_max: 3.0,
            rtt_p90: 2.5,
            rtt_p99: 2.9,
            packets_sent: 4,
            packets_lost: 1,
            packet_loss: 25.0,
            rtt_ports_active: 4,
            rtt_ports_total: 16,
            probe_cycle: 7,
            destination_batch_index: 1,
            port_batch_index: 2,
            df_packets_sent: 1,
            df_packets_lost: 0,
            df_mtu_drops: 0,
        };

        let lines = metric_lines("topology=1", &source, &target, &result, 123);

        assert_eq!(lines.len(), 1);
        assert_eq!(
            lines[0],
            "pingmesh,src_ip=192.0.2.1,dst_ip=192.0.2.2,src_name=client1,dst_name=client2,src_leaf=leaf1,dst_leaf=leaf2,path_type=cross_rack,topology_id=topology\\=1 rtt_min=1,rtt_avg=2,rtt_max=3,rtt_p90=2.5,rtt_p99=2.9,packets_sent=4i,packets_lost=1i,packet_loss=25,rtt_ports_active=4i,rtt_ports_total=16i,probe_cycle=7i,destination_batch_index=1i,port_batch_index=2i,df_packets_sent=1i,df_packets_lost=0i,df_mtu_drops=0i 123"
        );
    }
}

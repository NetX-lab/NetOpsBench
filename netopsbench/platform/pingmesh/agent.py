#!/usr/bin/env python3
"""Pingmesh probe agent library.

Defines :class:`PingmeshAgent`. The CLI entrypoint lives in
:mod:`netopsbench.platform.pingmesh.cli` (also copied into each client
container by :mod:`netopsbench.platform.pingmesh.deploy`).
"""

from __future__ import annotations

try:
    from netopsbench.platform.pingmesh._agent_influx import PingInfluxMixin
    from netopsbench.platform.pingmesh._agent_probe import UdpProbeMixin
    from netopsbench.platform.pingmesh._agent_responder import UdpEchoResponder
    from netopsbench.platform.pingmesh._agent_runtime import PingRuntimeMixin
    from netopsbench.platform.pingmesh._agent_support import (
        ThreadPoolExecutor,
        config,
        json,
        logger,
        os,
        queue,
        requests,
        threading,
        time,
    )
except ImportError:  # In-container deployment runs from /tmp/pingmesh/ flat files.
    from _agent_influx import PingInfluxMixin  # type: ignore[no-redef]
    from _agent_probe import UdpProbeMixin  # type: ignore[no-redef]
    from _agent_responder import UdpEchoResponder  # type: ignore[no-redef]
    from _agent_runtime import PingRuntimeMixin  # type: ignore[no-redef]
    from _agent_support import (  # type: ignore[no-redef]
        ThreadPoolExecutor,
        config,
        json,
        logger,
        os,
        queue,
        requests,
        threading,
        time,
    )


class PingmeshAgent(UdpProbeMixin, PingInfluxMixin, PingRuntimeMixin):
    """Pingmesh probe agent that runs periodic ping tests."""

    def __init__(
        self,
        pinglist_file: str,
        interval: float = 1.0,
        influxdb_url: str = None,
        influxdb_token: str = None,
        influxdb_org: str = None,
        influxdb_bucket: str = None,
        # UDP burst probe params (replaces former ICMP ping_count / ping_interval)
        n_rtt_ports: int = 16,
        n_df_ports: int = 4,
        udp_dst_port: int = 33434,
        rtt_src_port_base: int = 33000,
        df_src_port_base: int = 33100,
        burst_timeout_s: float = 1.0,
        enable_df_probe: bool = True,
        df_payload_size: int = 1400,
        min_interval: float = 1.0,
        max_interval: float = 1.0,
        batch_size: int = 50,
        batch_timeout: float = 2.0,
        max_retries: int = 3,
        retry_backoff_base: float = 0.5,
    ):
        self.interval = interval
        self.my_name = os.environ.get("HOSTNAME", "unknown")
        self.n_rtt_ports = n_rtt_ports
        self.n_df_ports = n_df_ports
        self.udp_dst_port = udp_dst_port
        self.rtt_src_port_base = rtt_src_port_base
        self.df_src_port_base = df_src_port_base
        self.burst_timeout_s = burst_timeout_s
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.enable_df_probe = enable_df_probe
        self.df_payload_size = df_payload_size
        self.influxdb_url = influxdb_url or config.influxdb_url
        self.influxdb_token = influxdb_token or config.influxdb_token
        self.influxdb_org = influxdb_org or config.influxdb_org
        self.influxdb_bucket = influxdb_bucket or config.influxdb_bucket
        self.use_influxdb = requests is not None
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base

        with open(pinglist_file, encoding="utf-8") as f:
            data = json.load(f)
        self.topology_id = config.topology_id or data.get("topology_id") or ""

        # Auto-derive DF payload size from topology MTU when using the default value.
        if self.df_payload_size == 1400:
            topo_mtu = self._infer_df_payload_from_topology()
            if topo_mtu is not None:
                self.df_payload_size = topo_mtu
                logger.info("  DF payload auto-derived from topology MTU: %s bytes", self.df_payload_size)

        self.tasks = [probe for probe in data["probes"] if probe["src_name"] == self.my_name]
        # Discover this host's data-plane IP from the pinglist so the UDP
        # responder binds to the fabric-facing interface (eth1), not mgmt.
        own_data_ip = next(
            (p["src_ip"] for p in data["probes"] if p["src_name"] == self.my_name),
            "",
        )
        self.responder = UdpEchoResponder(bind_ip=own_data_ip, port=self.udp_dst_port)
        try:
            self.responder.start()
        except Exception as exc:
            logger.error("Failed to start UDP echo responder: %s", exc)
            self.responder = None
        self.max_workers = min(max(len(self.tasks) // 10, 1), 16)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.write_queue = queue.Queue()
        self.batch_lock = threading.Lock()
        self.batch_buffer = []
        self.batch_last_flush = time.time()
        self.shutdown_event = threading.Event()
        if self.use_influxdb:
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "Authorization": f"Token {self.influxdb_token}",
                    "Content-Type": "text/plain; charset=utf-8",
                }
            )
            self.batch_writer_thread = threading.Thread(target=self._batch_writer_worker, daemon=True)
            self.batch_writer_thread.start()
        else:
            self.session = None

        logger.info("Pingmesh Agent started: %s", self.my_name)
        logger.info("  Probe tasks: %s", len(self.tasks))
        logger.info("  Parallel workers: %s", self.max_workers)
        logger.info(
            "  UDP RTT burst: %s ports (src %s-%s) -> dst :%s, timeout=%ss",
            self.n_rtt_ports,
            self.rtt_src_port_base,
            self.rtt_src_port_base + self.n_rtt_ports - 1,
            self.udp_dst_port,
            self.burst_timeout_s,
        )
        if self.enable_df_probe:
            logger.info(
                "  UDP DF burst: %s ports (src %s-%s), payload=%s bytes",
                self.n_df_ports,
                self.df_src_port_base,
                self.df_src_port_base + self.n_df_ports - 1,
                self.df_payload_size,
            )
        else:
            logger.info("  DF probe: disabled")
        if self.min_interval == self.max_interval:
            logger.info("  Fixed cycle interval: %ss", self.min_interval)
        else:
            logger.info("  Interval range: %s-%ss", self.min_interval, self.max_interval)
        logger.info("  Batch size: %s, timeout: %ss", self.batch_size, self.batch_timeout)
        logger.info("  InfluxDB mode: %s", "enabled" if self.use_influxdb else "disabled")

    def _infer_df_payload_from_topology(self):
        """Derive DF payload size from topology metadata MTU (minus IP+ICMP headers = 28 bytes)."""
        topo_dir = config.topology_dir or ""
        candidates = []
        if topo_dir:
            candidates.append(os.path.join(topo_dir, "topology.json"))
        # Fallback: look relative to project root
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        import glob as _glob

        for d in sorted(_glob.glob(os.path.join(base_dir, "lab-topology", "generated_topology_*")), reverse=True):
            candidates.append(os.path.join(d, "topology.json"))
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)
                defaults = meta.get("defaults", {})
                # Prefer sonic_port_mtu (actual device MTU) over link_mtu (container MTU)
                mtu = defaults.get("sonic_port_mtu") or defaults.get("link_mtu")
                if isinstance(mtu, int) and mtu > 28:
                    return mtu - 28  # subtract IP + ICMP header overhead
            except Exception:
                logger.debug("failed to read MTU metadata from %s", path, exc_info=True)
                continue
        return None

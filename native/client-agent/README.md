# NetOpsBench client agent

`netopsbench-client-agent` is the native data-plane helper shipped in every
NetOpsBench client container. One binary runs as two independent, single-thread
processes:

```text
netopsbench-client-agent pingmesh --config /etc/netopsbench/client-agent.json
netopsbench-client-agent traffic --config /etc/netopsbench/client-agent.json
```

The Pingmesh process owns UDP probing, kernel receive timestamps, and batched
writes to the topology-local Telegraf listener. The traffic process owns the
four planned background flows per client and exposes its control/status
contract on the management network.

The supported target is `x86_64-unknown-linux-musl`. The client image builds
the binary with the repository-pinned Rust toolchain and `Cargo.lock`; it does
not contain Python, iperf3, or a Rust toolchain at runtime.

Local checks:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
docker build -f containers/client/Dockerfile -t netopsbench-client:pingmesh-v2 .
```

Configuration v2 references immutable per-client `pinglists/<name>.json` files
and their SHA256 digests. The Python compiler selects all peers, ports, probe
orders and startup delays offline. Rust validates the files and repeats their
frames without generating or rotating peer lists. Every status response and
Pingmesh metric carries the current `plan_id`. Old configuration versions must
be regenerated with a matching rebuilt image.

DF confirmation keeps the planned peer and source port and sends fifteen
additional probes when the initial reply is missing. Retry rounds are spaced
10 ms apart, and probe replies are drained throughout sending to avoid local
receive queues turning transient loss into an MTU symptom.

Validate a client configuration without starting sockets:

```bash
HOSTNAME=client1 netopsbench-client-agent validate-config --config /etc/netopsbench/client-agent.json
```

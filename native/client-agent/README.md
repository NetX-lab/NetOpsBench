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

The supported v1 target is `x86_64-unknown-linux-musl`. The client image builds
the binary with the repository-pinned Rust toolchain and `Cargo.lock`; it does
not contain Python, iperf3, or a Rust toolchain at runtime.

Local checks:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
docker build -f containers/client/Dockerfile -t netopsbench-client:test .
```

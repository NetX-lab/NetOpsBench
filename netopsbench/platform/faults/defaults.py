"""Canonical defaults shared by fault adapters, handlers and generation."""

PACKET_LOSS_PCT = 30.0
PACKET_CORRUPTION_PCT = 20.0
LATENCY_MS = 120.0
MTU = 1400

IMPAIRMENT_PARAMETERS = {
    "packet_loss": {"loss_pct": PACKET_LOSS_PCT},
    "packet_corruption": {"corruption_pct": PACKET_CORRUPTION_PCT},
    "high_latency": {"latency_ms": LATENCY_MS},
    "mtu_mismatch": {"mtu": MTU},
}

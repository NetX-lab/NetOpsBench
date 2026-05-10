# Fault Extension Examples

This directory shows how to add a custom fault to NetOpsBench using the **public SDK surface** (`netopsbench.sdk.faults`).

## `custom_fault_pack/`

A minimal example that:

1. Implements a `FaultExecutor` with `inject()` / `recover()`.
2. Groups it in a `FaultPack` with a `FaultSpec`.
3. Registers it via `bench.faults.register_pack(pack)`.
4. References the custom fault from a scenario YAML.

```python
from netopsbench.sdk import NetOpsBench
from examples.faults.custom_fault_pack import build_fault_pack

bench = NetOpsBench(workspace=".")
bench.faults.register_pack(build_fault_pack())
# Now 'demo_custom_latency' is available in scenarios.
```

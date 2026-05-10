"""Tests for the public fault registry API."""

from dataclasses import dataclass
from types import SimpleNamespace

import netopsbench.sdk.faults as faults_api
from netopsbench.platform.faults.specs import FaultSpec, unregister_fault_spec


@dataclass
class _FakeFaultPack:
    name: str = "fake_pack"
    version: str = "1"

    def register(self, registry):
        registry.register(
            spec=FaultSpec(name="pack_fault"),
            executor=self,
        )

    def inject(self, context):
        return {"success": True, "operation": "inject", "fault_type": context.fault_type}

    def recover(self, context):
        return {"success": True, "operation": "recover", "fault_type": context.fault_type}


class _FakeExecutor:
    def inject(self, context):
        return {"success": True, "operation": "inject", "fault_type": context.fault_type}

    def recover(self, context):
        return {"success": True, "operation": "recover", "fault_type": context.fault_type}


def test_fault_manager_registers_custom_fault_spec_and_executor():
    manager = faults_api.FaultManager()
    spec = FaultSpec(name="custom_fault")

    try:
        manager.register(spec=spec, executor=_FakeExecutor())

        loaded = manager.get("custom_fault")
        assert loaded.name == "custom_fault"
        assert "custom_fault" in {item.name for item in manager.list()}
        assert manager.validate_parameters("custom_fault", {}) == []
    finally:
        unregister_fault_spec("custom_fault")


def test_fault_manager_load_builtin_exposes_builtin_faults():
    manager = faults_api.FaultManager()

    manager.load_builtin()

    names = {item.name for item in manager.list()}
    assert "static_route_misconfig" in names
    assert manager.get("static_route_misconfiguration").name == "static_route_misconfig"


def test_fault_manager_register_pack_adds_pack_faults():
    manager = faults_api.FaultManager()

    try:
        manager.register_pack(_FakeFaultPack())

        names = {item.name for item in manager.list()}
        assert "pack_fault" in names
    finally:
        unregister_fault_spec("pack_fault")


def test_fault_manager_validate_parameters_uses_minimal_adapter():
    seen = {}

    def _validator(episode):
        seen["has_target_device"] = hasattr(episode, "target_device")
        seen["has_target_interface"] = hasattr(episode, "target_interface")
        seen["has_target_prefix"] = hasattr(episode, "target_prefix")
        seen["has_parameters"] = hasattr(episode, "parameters")
        seen["has_metadata"] = hasattr(episode, "metadata")
        return []

    manager = faults_api.FaultManager()
    spec = FaultSpec(name="shape_fault", episode_validator=_validator)

    try:
        manager.register(spec=spec, executor=_FakeExecutor())

        assert manager.validate_parameters("shape_fault", {}) == []
        assert seen == {
            "has_target_device": False,
            "has_target_interface": True,
            "has_target_prefix": True,
            "has_parameters": True,
            "has_metadata": True,
        }
    finally:
        unregister_fault_spec("shape_fault")


def test_fault_manager_rejects_invalid_plugin_style_registration(monkeypatch):
    manager = faults_api.FaultManager()
    monkeypatch.setattr(
        faults_api,
        "import_module",
        lambda module_path: SimpleNamespace(BrokenPlugin=object()),
    )

    try:
        try:
            manager.load_plugin("fake.module:BrokenPlugin")
            raise AssertionError("expected TypeError")
        except TypeError as exc:
            assert "register()" in str(exc)
    finally:
        unregister_fault_spec("BrokenPlugin")


def test_custom_fault_example_registers_and_executes(tmp_path, monkeypatch):
    import subprocess

    from examples.faults.custom_fault_pack import FAULT_NAME, build_fault_pack
    from netopsbench.sdk import FaultContext, NetOpsBench

    # Mock subprocess.run so the test does not require a running Docker lab.
    monkeypatch.setattr(
        "examples.faults.custom_fault_pack.custom_fault.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    bench = NetOpsBench(workspace=str(tmp_path))
    bench.faults.register_pack(build_fault_pack())

    spec = bench.faults.get(FAULT_NAME)
    assert spec.name == FAULT_NAME

    executor = bench.faults.get_executor(FAULT_NAME)
    ctx = FaultContext(
        fault_type=FAULT_NAME,
        target_device="leaf1",
        target_interface="eth1",
        parameters={"delay_ms": 40, "jitter_ms": 8},
    )
    assert executor.inject(ctx).success is True
    assert executor.recover(ctx).success is True


def test_simple_fault_creates_dispatchable_pack(tmp_path):
    from netopsbench.platform.faults.specs import unregister_fault_spec
    from netopsbench.sdk import FaultContext, NetOpsBench, simple_fault

    calls = []

    def _inject(ctx: FaultContext) -> dict:
        calls.append(("inject", ctx.fault_type, ctx.target_device))
        return {"success": True, "device": ctx.target_device}

    def _recover(ctx: FaultContext) -> dict:
        calls.append(("recover", ctx.fault_type))
        return {"success": True}

    pack = simple_fault(
        "test_simple_latency",
        inject=_inject,
        recover=_recover,
        requires_interface=True,
        required_parameters=("delay_ms",),
    )
    bench = NetOpsBench(workspace=str(tmp_path))
    try:
        bench.faults.register_pack(pack)

        spec = bench.faults.get("test_simple_latency")
        assert spec.name == "test_simple_latency"
        assert spec.requires_interface is True

        executor = bench.faults.get_executor("test_simple_latency")
        ctx = FaultContext(
            fault_type="test_simple_latency",
            target_device="leaf2",
            target_interface="eth0",
            parameters={"delay_ms": 10},
        )
        inject_result = executor.inject(ctx)
        assert inject_result.success is True
        recover_result = executor.recover(ctx)
        assert recover_result.success is True
        assert calls == [
            ("inject", "test_simple_latency", "leaf2"),
            ("recover", "test_simple_latency"),
        ]
    finally:
        unregister_fault_spec("test_simple_latency")


def test_register_fault_shortcut(tmp_path):
    from netopsbench.platform.faults.specs import unregister_fault_spec
    from netopsbench.sdk import FaultContext, NetOpsBench

    def _inject(ctx: FaultContext) -> dict:
        return {"success": True}

    def _recover(ctx: FaultContext) -> dict:
        return {"success": True}

    bench = NetOpsBench(workspace=str(tmp_path))
    try:
        bench.faults.register_fault(
            "test_shortcut_fault",
            _inject,
            _recover,
            requires_interface=False,
        )

        spec = bench.faults.get("test_shortcut_fault")
        assert spec.name == "test_shortcut_fault"

        executor = bench.faults.get_executor("test_shortcut_fault")
        ctx = FaultContext(fault_type="test_shortcut_fault", target_device="spine1")
        assert executor.inject(ctx).success is True
        assert executor.recover(ctx).success is True
    finally:
        unregister_fault_spec("test_shortcut_fault")

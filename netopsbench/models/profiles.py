"""Data-driven benchmark scale profiles."""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class ClosTopologyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["clos"]
    spines: int = Field(gt=0)
    leafs: int = Field(gt=0)
    clients_per_leaf: int = Field(gt=0)


class FatTreeTopologyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["fat-tree"]
    k: int = Field(gt=0)
    clients_per_edge: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_fat_tree(self) -> FatTreeTopologyProfile:
        if self.k % 2:
            raise ValueError("fat-tree k must be even")
        if self.clients_per_edge > self.k // 2:
            raise ValueError("clients_per_edge cannot exceed k/2")
        return self


TopologyProfile = Annotated[ClosTopologyProfile | FatTreeTopologyProfile, Field(discriminator="family")]


class ManagementProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: int = Field(ge=16, le=30)
    subnet_base: int = Field(ge=0, le=255)


class PingmeshProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    destination_batch_size: int | None = Field(default=None, gt=0)
    rtt_port_pool_size: int = Field(gt=0)
    rtt_ports_per_cycle: int = Field(gt=0)
    cycle_interval_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_port_batches(self) -> PingmeshProfile:
        if self.rtt_ports_per_cycle > self.rtt_port_pool_size:
            raise ValueError("rtt_ports_per_cycle cannot exceed rtt_port_pool_size")
        return self


class TrafficProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_pps_per_client: int = Field(gt=0)


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deploy_timeout_seconds: int = Field(gt=0)
    worker_deploy_parallelism: int = Field(gt=0)
    health_timeout_seconds: int = Field(gt=0)
    containerlab_max_workers: int | None = Field(default=None, gt=0)


class ScaleProfile(BaseModel):
    """Validated scale facts shared by topology and runtime subsystems."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    topology: TopologyProfile
    management: ManagementProfile
    pingmesh: PingmeshProfile
    traffic: TrafficProfile
    runtime: RuntimeProfile

    @property
    def family(self) -> Literal["clos", "fat-tree"]:
        return self.topology.family

    @property
    def num_spines(self) -> int | None:
        return self.topology.spines if isinstance(self.topology, ClosTopologyProfile) else None

    @property
    def num_leafs(self) -> int | None:
        return self.topology.leafs if isinstance(self.topology, ClosTopologyProfile) else None

    @property
    def fat_tree_k(self) -> int | None:
        return self.topology.k if isinstance(self.topology, FatTreeTopologyProfile) else None

    @property
    def num_cores(self) -> int | None:
        return (self.topology.k // 2) ** 2 if isinstance(self.topology, FatTreeTopologyProfile) else None

    @property
    def num_aggs(self) -> int | None:
        return self.topology.k * (self.topology.k // 2) if isinstance(self.topology, FatTreeTopologyProfile) else None

    @property
    def num_edges(self) -> int | None:
        return self.num_aggs

    @property
    def clients_per_attached_switch(self) -> int:
        if isinstance(self.topology, ClosTopologyProfile):
            return self.topology.clients_per_leaf
        return self.topology.clients_per_edge

    @property
    def clients_per_leaf(self) -> int | None:
        return self.topology.clients_per_leaf if isinstance(self.topology, ClosTopologyProfile) else None

    @property
    def clients_per_edge(self) -> int | None:
        return self.topology.clients_per_edge if isinstance(self.topology, FatTreeTopologyProfile) else None

    @property
    def total_clients(self) -> int:
        attached_switches = self.num_leafs if self.family == "clos" else self.num_edges
        return int(attached_switches or 0) * self.clients_per_attached_switch

    @property
    def total_switches(self) -> int:
        if self.family == "clos":
            return int(self.num_spines or 0) + int(self.num_leafs or 0)
        return int(self.num_cores or 0) + int(self.num_aggs or 0) + int(self.num_edges or 0)

    @property
    def management_prefix(self) -> int:
        return self.management.prefix

    @property
    def management_subnet_base(self) -> int:
        return self.management.subnet_base

    @property
    def pingmesh_destination_batch_size(self) -> int | None:
        return self.pingmesh.destination_batch_size

    @property
    def pingmesh_rtt_port_pool_size(self) -> int:
        return self.pingmesh.rtt_port_pool_size

    @property
    def pingmesh_rtt_ports_per_cycle(self) -> int:
        return self.pingmesh.rtt_ports_per_cycle

    @property
    def pingmesh_cycle_interval_seconds(self) -> int:
        return self.pingmesh.cycle_interval_seconds

    @property
    def traffic_max_pps_per_client(self) -> int:
        return self.traffic.max_pps_per_client

    @property
    def deploy_timeout_seconds(self) -> int:
        return self.runtime.deploy_timeout_seconds

    @property
    def worker_deploy_parallelism(self) -> int:
        return self.runtime.worker_deploy_parallelism

    @property
    def health_timeout_seconds(self) -> int:
        return self.runtime.health_timeout_seconds

    @property
    def containerlab_max_workers(self) -> int | None:
        return self.runtime.containerlab_max_workers

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScaleProfileDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    override: bool = False
    profiles: list[ScaleProfile]


class ScaleRegistry(BaseModel):
    """Ordered, validated collection of scale profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: tuple[ScaleProfile, ...]

    @model_validator(mode="after")
    def validate_profiles(self) -> ScaleRegistry:
        names: set[str] = set()
        for profile in self.profiles:
            if profile.name in names:
                raise ValueError(f"Duplicate scale profile: {profile.name}")
            names.add(profile.name)
        if not self.profiles:
            raise ValueError("Scale registry must contain at least one profile")
        return self

    @cached_property
    def _profile_map(self) -> dict[str, ScaleProfile]:
        return {profile.name: profile for profile in self.profiles}

    @classmethod
    def from_file(cls, path: str | Path) -> ScaleRegistry:
        document = _load_profile_document(path)
        return cls(profiles=tuple(document.profiles))

    @classmethod
    def with_builtins(cls, profile_files: list[str | Path] | tuple[str | Path, ...] = ()) -> ScaleRegistry:
        registry = cls(profiles=tuple(_builtin_document().profiles))
        for path in profile_files:
            registry = registry.merged(_load_profile_document(path))
        return registry

    def merged(self, document: ScaleProfileDocument) -> ScaleRegistry:
        profiles = dict(self._profile_map)
        for profile in document.profiles:
            if profile.name in profiles and not document.override:
                raise ValueError(
                    f"Scale profile {profile.name!r} already exists; set override: true in the profile file"
                )
            profiles[profile.name] = profile
        return ScaleRegistry(profiles=tuple(profiles.values()))

    def get(self, name: str) -> ScaleProfile:
        try:
            return self._profile_map[name]
        except KeyError as exc:
            raise ValueError(f"Unknown scale: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._profile_map)

    def values(self) -> tuple[ScaleProfile, ...]:
        return tuple(self._profile_map.values())

    @cached_property
    def digest(self) -> str:
        payload = [profile.model_dump(mode="json") for profile in self.profiles]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_DOCUMENT_ADAPTER = TypeAdapter(ScaleProfileDocument)


def _load_profile_document(path: str | Path) -> ScaleProfileDocument:
    profile_path = Path(path)
    try:
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to load scale profile file {profile_path}: {exc}") from exc
    return _DOCUMENT_ADAPTER.validate_python(payload)


def _builtin_document() -> ScaleProfileDocument:
    resource = files("netopsbench.models").joinpath("scale_profiles.yaml")
    payload = yaml.safe_load(resource.read_text(encoding="utf-8"))
    return _DOCUMENT_ADAPTER.validate_python(payload)


_DEFAULT_REGISTRY = ScaleRegistry.with_builtins()
SCALE_PROFILES: dict[str, ScaleProfile] = {profile.name: profile for profile in _DEFAULT_REGISTRY.values()}


def default_scale_registry() -> ScaleRegistry:
    return _DEFAULT_REGISTRY


def supported_scales(registry: ScaleRegistry | None = None) -> tuple[str, ...]:
    return (registry or _DEFAULT_REGISTRY).names()


def get_scale_profile(name: str, registry: ScaleRegistry | None = None) -> ScaleProfile:
    return (registry or _DEFAULT_REGISTRY).get(name)


__all__ = [
    "ClosTopologyProfile",
    "FatTreeTopologyProfile",
    "ManagementProfile",
    "PingmeshProfile",
    "RuntimeProfile",
    "ScaleProfile",
    "ScaleProfileDocument",
    "ScaleRegistry",
    "SCALE_PROFILES",
    "TrafficProfile",
    "default_scale_registry",
    "get_scale_profile",
    "supported_scales",
]

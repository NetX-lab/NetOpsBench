"""Persisted, offline-compiled Pingmesh schedules."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProbeFrame(_PlanModel):
    destination_batch_index: int = Field(ge=0)
    port_batch_index: int = Field(ge=0)
    target_indices: list[int]
    # Kind, frame-local target index, batch-local socket index.
    probes: list[tuple[Literal["rtt", "df"], int, int]]


class ClientProbePlan(_PlanModel):
    name: str
    targets: list[str]
    startup_delay_ms: int = Field(ge=0)
    frames: list[ProbeFrame]


class PlanSettings(_PlanModel):
    plan_id: str
    source_ports: list[int]
    port_batches: list[list[int]]
    destination_batch_size: int = Field(gt=0)
    cycle_interval_seconds: int = Field(gt=0)
    coverage_epoch_cycles: int = Field(gt=0)
    coverage_epoch_seconds: int = Field(gt=0)
    complete_window_seconds: int = Field(gt=0)
    df_payload_size: int = Field(gt=0)


class LinkCoverage(_PlanModel):
    source: str
    destination: str
    source_interface: str
    destination_interface: str
    witnesses: int = Field(ge=0)


class CandidateCoverage(_PlanModel):
    basis: Literal["topology_candidate_paths"] = "topology_candidate_paths"
    directed_links: list[LinkCoverage]
    covered_switches: list[str]
    minimum_witnesses: int = Field(ge=0)


class PingmeshPlan(_PlanModel):
    schema_version: Literal["1"] = "1"
    settings: PlanSettings
    clients: list[ClientProbePlan]
    coverage: CandidateCoverage

    @property
    def directed_pair_count(self) -> int:
        return sum(len(client.targets) for client in self.clients)

    def summary(self) -> dict:
        return {
            **self.settings.model_dump(mode="json"),
            "directed_pair_count": self.directed_pair_count,
            "coverage_basis": self.coverage.basis,
            "candidate_directed_links": len(self.coverage.directed_links),
            "minimum_link_witnesses": self.coverage.minimum_witnesses,
        }

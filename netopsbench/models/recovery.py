"""Versioned recovery policy stored in scenario metadata, never scale digests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    mode: Literal["full_window", "state_checked"] = "state_checked"
    baseline_max_cases: int = Field(default=5, gt=0)
    baseline_max_age_seconds: int = Field(default=600, gt=0)

    @classmethod
    def from_metadata(cls, metadata: dict) -> "RecoveryPolicy":
        value = metadata.get("recovery_policy")
        return cls(mode="full_window") if value is None else cls.model_validate(value)

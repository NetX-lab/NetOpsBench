"""Versioned recovery policy stored in scenario metadata, never scale digests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = 2
    baseline_max_cases: int = Field(default=10, gt=0)
    baseline_max_age_seconds: int = Field(default=600, gt=0)

    @classmethod
    def from_metadata(cls, metadata: dict) -> "RecoveryPolicy":
        value = metadata.get("recovery_policy")
        if not isinstance(value, dict) or value.get("version") != 2:
            raise ValueError(
                "Recovery policy v2 required; migrate with netopsbench scenario migrate SOURCE --out NEW_DIRECTORY"
            )
        return cls.model_validate(value)

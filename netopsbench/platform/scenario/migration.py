"""Explicit, non-destructive migration of frozen scenarios to recovery v3."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from netopsbench.models.recovery import RecoveryPolicy
from netopsbench.platform.scenario.parser import parse_scenario_file, save_scenario_file


def migrate_recovery(source: Path, output: Path) -> Path:
    source, output = source.resolve(), output.resolve()
    if output.exists() or source == output or (source.is_dir() and source in output.parents):
        raise ValueError("Migration requires a new output directory outside the source")
    paths = [source] if source.is_file() else sorted({*source.rglob("*.yaml"), *source.rglob("*.yml")})
    if not paths:
        raise ValueError("No scenario YAML files found")
    scenarios = [(path, parse_scenario_file(path)) for path in paths]
    provenance = [
        {
            "path": str(p.relative_to(source) if source.is_dir() else p.name),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "scenario_digest": s.digest,
        }
        for p, s in scenarios
    ]
    policy = RecoveryPolicy().model_dump(mode="json")
    campaign_id = hashlib.sha256(
        json.dumps({"sources": provenance, "recovery_policy": policy}, sort_keys=True).encode()
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".recovery-migration-", dir=output.parent))
    try:
        for (_, scenario), origin in zip(scenarios, provenance, strict=True):
            metadata = {
                **scenario.metadata,
                "recovery_policy": policy,
                "recovery_migration": {"campaign_id": campaign_id, "source": origin},
            }
            migrated = scenario.model_copy(update={"metadata": metadata})
            save_scenario_file(migrated, staging / origin["path"])
        (staging / "migration.json").write_text(
            json.dumps(
                {
                    "campaign_id": campaign_id,
                    "recovery_policy": policy,
                    "sources": provenance,
                },
                indent=2,
            )
            + "\n"
        )
        if source.is_dir() and (source / "coverage.json").is_file():
            shutil.copyfile(source / "coverage.json", staging / "coverage.json")
        # Exclusive reservation prevents replacing a concurrently-created destination.
        output.mkdir()
        try:
            for path in staging.iterdir():
                path.rename(output / path.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    finally:
        shutil.rmtree(staging)
    return output

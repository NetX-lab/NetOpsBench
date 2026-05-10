"""
Pingmesh Pinglist Generator
Generates probe task lists from topology metadata
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class ProbeTask:
    """Single probe task specification"""

    src_ip: str
    src_name: str
    src_rack: str
    src_leaf: str
    dst_ip: str
    dst_name: str
    dst_rack: str
    dst_leaf: str
    path_type: str  # "same_rack" | "cross_rack"


class PinglistGenerator:
    """Generates pinglist for all client-to-client pairs"""

    def generate(self, topology_metadata: dict) -> list[ProbeTask]:
        """
        Generate N×N probe tasks for all client pairs.

        Args:
            topology_metadata: Topology metadata dict with devices.clients

        Returns:
            List of ProbeTask objects for all client pairs
        """
        clients = topology_metadata["devices"]["clients"]
        tasks = []

        for src in clients:
            for dst in clients:
                if src["name"] == dst["name"]:
                    continue  # Skip self-to-self

                # Determine path type
                path_type = "same_rack" if src["rack"] == dst["rack"] else "cross_rack"

                task = ProbeTask(
                    src_ip=src["data_ip"],
                    src_name=src["name"],
                    src_rack=src["rack"],
                    src_leaf=src["leaf"],
                    dst_ip=dst["data_ip"],
                    dst_name=dst["name"],
                    dst_rack=dst["rack"],
                    dst_leaf=dst["leaf"],
                    path_type=path_type,
                )
                tasks.append(task)

        return tasks

    def save_pinglist(
        self,
        tasks: list[ProbeTask],
        output_file: str,
        topology_id: str | None = None,
    ):
        """
        Save pinglist to JSON file.

        Args:
            tasks: List of ProbeTask objects
            output_file: Output JSON file path
        """
        data = {"total_probes": len(tasks), "probes": [asdict(t) for t in tasks]}
        if topology_id:
            data["topology_id"] = topology_id

        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)

        logger.info("Saved %s probe tasks to %s", len(tasks), output_file)


def _infer_topology_id(topology_file: str) -> str:
    return Path(topology_file).resolve().parent.name


def generate_pinglist_from_topology(
    topology_file: str,
    output_file: str = "pinglist.json",
    topology_id: str | None = None,
):
    """
    Convenience function to generate pinglist from topology file.

    Args:
        topology_file: Path to topology.json
        output_file: Output pinglist.json path
        topology_id: Explicit topology identifier; inferred from directory name if omitted
    """
    with open(topology_file) as f:
        topology = json.load(f)

    generator = PinglistGenerator()
    tasks = generator.generate(topology)
    topology_id = topology_id or _infer_topology_id(topology_file)
    generator.save_pinglist(tasks, output_file, topology_id=topology_id)

    return tasks

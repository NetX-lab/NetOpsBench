"""Influx/topology helpers for Pingmesh anomaly detector."""

from __future__ import annotations

import csv
import glob
import json
import os
from io import StringIO

import requests

from netopsbench.config import config

try:
    from netopsbench.logging_utils import get_logger
except ModuleNotFoundError:
    import logging

    def get_logger(name: str):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        return logging.getLogger(name)


logger = get_logger(__name__)


class DetectorQueryMixin:
    def _query_influxdb(self, query: str) -> list[dict]:
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        }
        try:
            self.last_query_error = None
            response = requests.post(
                f"{self.influxdb_url}/api/v2/query?org={self.org}",
                headers=headers,
                data=query,
                timeout=30,
                proxies={"http": "", "https": ""},
            )
            response.raise_for_status()
            results = []
            csv_data = StringIO(response.text)
            reader = csv.DictReader(csv_data)
            for row in reader:
                if row.get("result", "") == "result" or not row.get("_value"):
                    continue
                try:
                    row["value"] = float(row["_value"])
                except (ValueError, KeyError):
                    continue
                results.append(row)
            return results
        except Exception as e:
            self.last_query_error = str(e)
            logger.error("InfluxDB query error: %s", e)
            return []

    def _topology_filter(self) -> str:
        if not self.topology_id:
            return ""
        safe = str(self.topology_id).replace("\\", "\\\\").replace('"', '\\"')
        return f'  |> filter(fn: (r) => r.topology_id == "{safe}")\n'

    def _load_topology_metadata(self, metadata: dict) -> None:
        devices = metadata.get("devices", {}) if isinstance(metadata, dict) else {}
        clients = devices.get("clients", []) if isinstance(devices, dict) else []
        for client in clients:
            name = client.get("name")
            leaf = client.get("leaf")
            if isinstance(name, str) and isinstance(leaf, str) and name and leaf:
                self.client_to_leaf[name] = leaf

        # Build leaf → spines mapping from topology links or spine/leaf lists.
        spines = devices.get("spines", []) if isinstance(devices, dict) else []
        leafs = devices.get("leafs", []) if isinstance(devices, dict) else []
        spine_names = [s.get("name") for s in spines if isinstance(s, dict) and s.get("name")]
        if not hasattr(self, "leaf_to_spines"):
            self.leaf_to_spines = {}
        links = metadata.get("links", []) if isinstance(metadata, dict) else []
        if links:
            spine_set = set(spine_names)
            for link in links:
                if not isinstance(link, dict):
                    continue
                endpoints = link.get("endpoints", [])
                if len(endpoints) != 2:
                    continue
                a, b = endpoints[0], endpoints[1]
                a_name = a.get("device") if isinstance(a, dict) else None
                b_name = b.get("device") if isinstance(b, dict) else None
                if a_name in spine_set and b_name not in spine_set:
                    self.leaf_to_spines.setdefault(b_name, [])
                    if a_name not in self.leaf_to_spines[b_name]:
                        self.leaf_to_spines[b_name].append(a_name)
                elif b_name in spine_set and a_name not in spine_set:
                    self.leaf_to_spines.setdefault(a_name, [])
                    if b_name not in self.leaf_to_spines[a_name]:
                        self.leaf_to_spines[a_name].append(b_name)
        elif spine_names and leafs:
            # Full-mesh assumption: every leaf connects to every spine.
            for leaf in leafs:
                leaf_name = leaf.get("name") if isinstance(leaf, dict) else None
                if leaf_name:
                    self.leaf_to_spines[leaf_name] = list(spine_names)

    def _load_topology_metadata_from_disk(self) -> dict | None:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        env_dir = config.topology_dir
        candidates = []
        if env_dir:
            candidates.append(os.path.join(env_dir, "topology.json"))
        generated_dirs = sorted(
            glob.glob(os.path.join(base_dir, "lab-topology", "generated_topology_*")),
            key=os.path.getmtime,
            reverse=True,
        )
        for candidate_dir in generated_dirs:
            candidates.append(os.path.join(candidate_dir, "topology.json"))
        candidates.append(os.path.join(base_dir, "lab-topology", "topology.json"))
        for metadata_file in candidates:
            if not os.path.exists(metadata_file):
                continue
            try:
                with open(metadata_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.debug("failed to read topology metadata %s", metadata_file, exc_info=True)
                continue
        return None

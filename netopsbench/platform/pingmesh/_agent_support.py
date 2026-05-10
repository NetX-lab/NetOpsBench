"""Shared bootstrap/runtime dependencies for Pingmesh agent."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    requests = None

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


try:
    from netopsbench.config import config
except ModuleNotFoundError:

    class _StandaloneConfig:
        influxdb_url = os.environ.get("NETOPSBENCH_INFLUXDB_URL", "http://influxdb:8086")
        influxdb_token = os.environ.get("NETOPSBENCH_INFLUXDB_TOKEN", "replace-me")
        influxdb_org = os.environ.get("NETOPSBENCH_INFLUXDB_ORG", "netopsbench")
        influxdb_bucket = os.environ.get("NETOPSBENCH_INFLUXDB_BUCKET", "netopsbench")
        topology_id = os.environ.get("NETOPSBENCH_TOPOLOGY_ID", "")
        topology_dir = os.environ.get("NETOPSBENCH_TOPOLOGY_DIR", "")
        pingmesh_influxdb_url = os.environ.get("NETOPSBENCH_PINGMESH_INFLUXDB_URL", "http://influxdb:8086")

    config = _StandaloneConfig()

logger = get_logger(__name__)

__all__ = [
    "ThreadPoolExecutor",
    "as_completed",
    "config",
    "json",
    "logger",
    "os",
    "queue",
    "re",
    "requests",
    "subprocess",
    "threading",
    "time",
]

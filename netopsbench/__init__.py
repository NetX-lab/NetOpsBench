"""
NetOpsBench - DCN Fault Troubleshooting Benchmark System

A benchmark system for evaluating AI Agents on datacenter network fault diagnosis.
"""

__version__ = "0.1.0"

__all__ = ["NetOpsBench", "__version__"]


def __getattr__(name):
    if name == "NetOpsBench":
        from netopsbench.sdk import NetOpsBench

        return NetOpsBench
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

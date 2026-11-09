"""Private, mechanism-specific validity checks over actual observations.

This module is never used to construct agent prompts or tool results. Public
route changes are computed separately without access to episode parameters.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from datetime import UTC, datetime, timedelta

from netopsbench.models.profiles import get_scale_profile
from netopsbench.platform.faults.defaults import LATENCY_MS
from netopsbench.platform.incident.context import extract_episode_pingmesh_query_window
from netopsbench.platform.observability.routes import next_hops
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.utils.control import OperationControl, current_operation_control
from netopsbench.platform.utils.proc import docker_prefix, safe_run


def _route(snapshot, device, prefix):
    network = ipaddress.ip_network(prefix, strict=False)
    return next(
        (
            route
            for route in snapshot["nodes"].get(device, {}).get("fib", [])
            if route.get("dst") not in {None, "default"} and ipaddress.ip_network(route["dst"], strict=False) == network
        ),
        None,
    )


def _count(snapshot, device, prefix):
    route = _route(snapshot, device, prefix)
    return len(next_hops(route)) if route and route.get("type", "unicast") == "unicast" else 0


def _measured_loss(rows, probe="regular"):
    return any(
        (row.get("fault", {}).get(probe) or {}).get("lost", 0) >= 3
        and (row.get("fault", {}).get(probe) or {}).get("loss_pct", 0)
        > (row.get("baseline", {}).get(probe) or {}).get("loss_pct", 0)
        for row in rows
    )


def _periodic_client_outages(phases, clients, end_time):
    """Bound access outages by verified down/up transactions, including transitions."""
    end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    outages = []
    for index, phase in enumerate(phases):
        if phase["phase"] != "fault":
            continue
        start = datetime.fromisoformat(phase.get("started_at", phase["time"]).replace("Z", "+00:00"))
        stop = end
        if index + 1 < len(phases):
            stop = min(stop, datetime.fromisoformat(phases[index + 1]["time"].replace("Z", "+00:00")))
        if start < stop:
            outages.extend(
                {"client": client, "start": start.isoformat(), "end": stop.isoformat()} for client in clients
            )
    return outages


def _observed_periodic_phases(history, start_time, end_time):
    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    previous, observed = [], []
    for phase in history:
        verified_at = datetime.fromisoformat(phase["time"].replace("Z", "+00:00"))
        if verified_at <= start:
            previous = [phase]
        elif verified_at <= end:
            observed.append(phase)
    return previous + observed


def _outage_explains_local_error(row, outages, cycle_seconds):
    if row.get("socket_batch_valid") is not True or not (row.get("local_probe_errors") or 0) > 0:
        return False
    try:
        end = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        start = end - timedelta(seconds=cycle_seconds)
        # Native metrics are timestamped after the fixed probe cycle. A cycle
        # can straddle a verified link transition; limit that overlap to one
        # plan interval rather than excusing the client's whole episode.
        return any(
            row["src"] == outage["client"]
            and start < datetime.fromisoformat(outage["end"])
            and end > datetime.fromisoformat(outage["start"])
            for outage in outages
        )
    except (KeyError, TypeError, ValueError):
        return False


def _ttl_loop_evidence(executor, episode, baseline, current):
    root, prefix = episode.target_device, episode.target_prefix
    evidence = {"forwarding_cycle": False, "ttl_expirations": {}}
    if not prefix or not episode.target_interface:
        return evidence
    endpoints = executor.injector._link._link_endpoints(root, episode.target_interface)
    peer = endpoints[1][0]
    root_route = _route(current, root, prefix)
    target = ipaddress.ip_network(prefix).network_address
    candidates = []
    for route in current["nodes"].get(peer, {}).get("fib", []):
        dst = route.get("dst") or "default"
        network = ipaddress.ip_network("0.0.0.0/0" if dst == "default" else dst, strict=False)
        if target in network:
            candidates.append((network.prefixlen, route))
    peer_route = max(candidates, key=lambda item: item[0], default=(0, None))[1]
    local = next(
        (
            r.get("prefsrc")
            for r in baseline["nodes"].get(root, {}).get("fib", [])
            if r.get("protocol") == "kernel" and r.get("dev") == endpoints[0][2]
        ),
        None,
    )
    evidence["forwarding_cycle"] = bool(
        local
        and root_route
        and peer_route
        and episode.parameters.get("next_hop") in {h.get("gateway") for h in next_hops(root_route)}
        and local in {h.get("gateway") for h in next_hops(peer_route)}
    )
    for device in (root, peer):
        before = baseline["nodes"].get(device, {}).get("packet_counters", {})
        after = current["nodes"].get(device, {}).get("packet_counters", {})
        key = "icmp_out_time_exceeded"
        if key in before and key in after and after[key] > before[key]:
            evidence["ttl_expirations"][device] = {"before": before[key], "after": after[key]}
    return evidence


def validate_effect(executor, episode, episode_result, baseline, current):
    fault, root, prefix = episode.fault_type, episode.target_device, episode.target_prefix
    p = episode.parameters
    mechanism = p.get("mechanism", fault)
    errors, evidence = [], {"collected_at": datetime.now(UTC).isoformat(), "mechanism": mechanism}
    node_errors = {name: row["error"] for name, row in current["nodes"].items() if row.get("error")}
    if node_errors and not (fault == "device_down" and set(node_errors) == {root}):
        errors.append(f"Unexpected route collection failures: {node_errors}")
    bgp_errors = {name for name, row in current["nodes"].items() if row.get("bgp_collection_ok") is False}
    if bgp_errors and not (fault in {"device_down", "bgp_process_down"} and bgp_errors == {root}):
        errors.append(f"Unexpected BGP table collection failures: {sorted(bgp_errors)}")
    toolkit = AgentToolkit(topology_metadata=executor.current_manifest().model_dump(mode="json"))
    toolkit.influxdb_bucket = executor.influxdb_bucket
    toolkit.topology_id = executor.topology_id
    injection = episode_result.get("injection", {})
    quality = episode_result.get("observations", {}).get("pingmesh_metrics", {}).get("quality", {})
    probes = episode_result.get("observations", {}).pop("_probe_evidence", [])
    evidence["probes"] = probes
    validation_only = executor.current_manifest().purpose == "validation"
    evidence["control_role"] = "comparison" if validation_only else "unaffected"
    by_pair = {(row["src"], row["dst"]): row for row in probes}
    affected_pairs = executor._case_affected_pairs
    affected_rows = [by_pair.get((pair["src"], pair["dst"]), {}) for pair in affected_pairs]
    for pair in affected_pairs + p.get("control_candidates", []):
        row = by_pair.get((pair["src"], pair["dst"]), {})
        if not row.get("baseline", {}).get("samples"):
            errors.append(f"Planned witness lacks healthy probe evidence: {pair}")
    status = toolkit.get_device_status(root) if root else None
    if status:
        evidence["device_status"] = status.to_dict()
    if fault == "none":
        return {"valid": not errors, "errors": errors, "evidence": evidence, "unobservable_clients": []}
    if status is None or not status.success:
        errors.append("Actual node status unavailable")
    elif fault == "device_down":
        if status.data["running"]:
            errors.append("Root container remains running")
    elif not status.data["running"] or not status.data["accessible"]:
        errors.append("Root is unexpectedly unavailable")
    if fault.startswith("bgp_") and fault != "bgp_process_down":
        neighbor = toolkit.get_bgp_neighbor(root, p["peer_ip"])
        evidence["neighbor"] = neighbor.to_dict()
        if not neighbor.success or str(neighbor.data.get("state", "")).lower() == "established":
            errors.append("Target BGP session did not leave Established")
        elif fault == "bgp_max_prefix_misconfig":
            reasons = {key: neighbor.data.get(key) for key in ("last_reset", "last_error", "notifications")}
            evidence["maximum_prefix_reason"] = reasons
            if not re.search(
                r"(?:maximum|exceed|limit).*prefix|prefix.*(?:exceed|limit)|max-prefix", json.dumps(reasons), re.I
            ):
                errors.append("BGP maximum-prefix trigger was not observed")
    if fault == "bgp_process_down":
        if not status or not status.success or status.data.get("services", {}).get("bgpd") != "STOPPED":
            errors.append("bgpd was not stopped under Supervisor")
    if fault in {"route_origination_misconfig", "route_filter_misconfig", "ecmp_misconfig"}:
        changed = [
            device for device in baseline["nodes"] if _count(current, device, prefix) < _count(baseline, device, prefix)
        ]
        evidence["changed_forwarding_devices"] = changed
        if not changed:
            errors.append("No actual FIB effect for target prefix")
        if fault == "ecmp_misconfig" or mechanism == "import_filter":
            before, after = _count(baseline, root, prefix), _count(current, root, prefix)
            evidence.update(next_hops_before=before, next_hops_after=after)
            reduced = fault == "ecmp_misconfig" or p.get("impact_scope", "") in {"reduced", "reduced_remote_pod"}
            if reduced and not 0 < after < before:
                errors.append("Expected nonzero forwarding with fewer next hops")
            if not reduced and after != 0:
                errors.append("Expected complete target prefix withdrawal")
        if fault == "ecmp_misconfig":
            rib = toolkit.get_bgp_rib(root, prefix)
            sessions = toolkit.get_bgp_neighbors(root)
            evidence.update(candidate_bgp_routes=rib.to_dict(), sessions=sessions.to_dict())
            if (
                not rib.success
                or not sessions.success
                or any(n.get("state") != "Established" for n in sessions.data.get("neighbors", []))
            ):
                errors.append("ECMP route/session evidence is incomplete")
    if fault == "routing_loop":
        witnesses = affected_pairs
        ttl = _ttl_loop_evidence(executor, episode, baseline, current)
        evidence["ttl_exhaustion"] = ttl
        traces, cyclic = [], bool(ttl["forwarding_cycle"] and ttl["ttl_expirations"])
        control = current_operation_control() or OperationControl(
            getattr(executor, "_cancel_event", None),
            time.monotonic() + get_scale_profile(executor.current_manifest().scale).health_timeout_seconds,
        )
        for pair in witnesses[:8]:
            if cyclic:
                break
            dst = executor.current_manifest().device(pair["dst"]).data_ip
            container = toolkit._resolve_container(pair["src"])
            # ICMP does not bind the UDP sockets already owned by Pingmesh.
            # This proves an actual cycle, without claiming ECMP calibration.
            output = safe_run(
                [
                    *docker_prefix(),
                    "docker",
                    "exec",
                    container,
                    "traceroute",
                    "-n",
                    "-I",
                    "-q",
                    "1",
                    "-w",
                    "0.2",
                    "-m",
                    "32",
                    dst,
                ],
                timeout=control.remaining(15),
                control=control,
            )
            hops = [
                re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)[0]
                for line in output.stdout.splitlines()[1:]
                if re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
            ]
            traces.append(
                {
                    "src": pair["src"],
                    "dst": pair["dst"],
                    "protocol": "icmp",
                    "output": output.stdout,
                    "returncode": output.returncode,
                }
            )
            cyclic = len(hops) >= 4 and len(set(hops)) < len(hops)
            if cyclic:
                break
        evidence["traces"] = traces
        if not cyclic:
            errors.append("No actual forwarding cycle or TTL exhaustion was proved")
    unobservable, access_clients, bounded_outages = [], [], []
    if fault == "device_down" and status and status.success and not status.data["running"]:
        unobservable = [c.name for c in executor.current_manifest().clients() if c.attached_switch == root]
    if fault in {
        "link_down",
        "interface_ip_misconfig",
        "link_flapping",
        "packet_loss",
        "packet_corruption",
        "high_latency",
        "mtu_mismatch",
    }:
        interface = executor.injector._iface.resolve_sonic(episode.target_interface)
        details = toolkit.get_device_interfaces(root, interface=interface)
        evidence["interface"] = details.to_dict()
        if not details.success:
            errors.append("Actual interface configuration unavailable")
        if fault in {"packet_loss", "packet_corruption", "high_latency"}:
            # Injection state belongs to the private effect audit, never to
            # AgentToolkit or the model-facing diagnostic registry.
            linux_interface = executor.injector._iface.resolve_linux(interface)
            try:
                qdisc = executor.injector._cmd.docker_exec(
                    executor.injector.container_names[root],
                    ["tc", "-s", "-j", "qdisc", "show", "dev", linux_interface],
                )
                if qdisc.returncode:
                    raise RuntimeError(qdisc.stderr or "qdisc collection failed")
                evidence["qdisc"] = {"linux_interface": linux_interface, "rows": json.loads(qdisc.stdout)}
            except Exception as exc:
                errors.append(f"Private qdisc collection failed: {exc}")
        if fault == "interface_ip_misconfig" and details.success:
            addresses = details.data.get("addresses", {}).get(interface, [])
            if injection.get("evidence", {}).get("fault_address") not in addresses:
                errors.append("Actual interface address did not change")
        if fault == "link_down" and details.success:
            rows = details.data.get("interfaces", [])
            selected = next((row for row in rows if row.get("name") == interface), {})
            if str(selected.get("admin", selected.get("state", ""))).lower() not in {"down", "disabled"}:
                errors.append("Link admin-down state unavailable")
        if fault in {"link_down", "interface_ip_misconfig", "link_flapping"} and not errors:
            for link in executor.current_manifest().links:
                ends = link.endpoints
                for endpoint, peer in (ends, tuple(reversed(ends))):
                    if (
                        endpoint.device == root
                        and executor.injector._iface.resolve_sonic(endpoint.interface) == interface
                    ):
                        node = executor.current_manifest().device(peer.device)
                        if node.role.value == "client":
                            access_clients.append(node.name)
            if fault != "link_flapping":
                unobservable.extend(access_clients)
        if fault in {"packet_loss", "packet_corruption", "high_latency", "mtu_mismatch"}:
            if fault == "high_latency":
                observed = any(
                    (row.get("fault", {}).get("rtt_max_ms") or 0) - (row.get("baseline", {}).get("rtt_max_ms") or 0)
                    >= float(p.get("latency_ms", LATENCY_MS)) / 2
                    for row in affected_rows
                )
            else:
                probe = "df" if fault == "mtu_mismatch" else "regular"
                observed = _measured_loss(affected_rows, probe)
            if not observed:
                errors.append("Configured impairment had no measured effect on planned witnesses")
    if fault in {"route_flapping", "link_flapping"}:
        identifier = injection.get("control_id", injection.get("task_id"))
        controller = executor.injector._tracker._background_fault_controls.get(identifier, {}).get("controller")
        scope = episode_result["observations"]
        history = list(controller.history) if controller else []
        phases = _observed_periodic_phases(history, scope["start_time"], scope["end_time"]) if controller else []
        transitions = [p["phase"] for p in phases]
        repeated = any(transitions[i : i + 3] == ["fault", "healthy", "fault"] for i in range(len(transitions) - 2))
        if controller is None or controller.error or not repeated:
            errors.append("Periodic fault did not complete multiple verified phases")
        if controller:
            evidence["phases"] = phases
            evidence["controller_error"] = controller.error
            if controller.error:
                errors.append(f"Periodic controller failed: {controller.error}")
            if fault == "link_flapping" and not controller.error and repeated:
                bounded_outages = _periodic_client_outages(
                    history, access_clients, episode_result["observations"]["end_time"]
                )
        if fault == "route_flapping":
            scope = extract_episode_pingmesh_query_window(episode_result)
            interface = executor.injector._iface.resolve_sonic(episode.target_interface)
            addresses = toolkit.get_device_interfaces(root, interface=interface)
            local_addresses = addresses.data.get("addresses", {}).get(interface, []) if addresses.success else []
            peer_address = local_addresses[0].split("/")[0] if local_addresses else None
            peer_devices = {
                endpoint.device
                for link in executor.current_manifest().links
                if any(
                    end.device == root and executor.injector._iface.resolve_sonic(end.interface) == interface
                    for end in link.endpoints
                )
                for endpoint in link.endpoints
                if endpoint.device != root
            }
            events = toolkit.query_bgp_events(
                **scope,
                device=next(iter(sorted(peer_devices)), None),
                peer=peer_address,
                event_type="prefix_count",
                state="all",
                limit=500,
            )
            evidence["prefix_events"] = events.to_dict()
            rows = events.data.get("events", []) if events.success else []
            rows = [row for row in rows if row.get("device") in peer_devices and row.get("peer") == peer_address]
            changes = [
                row["prefixes_after"] - row["prefixes_before"]
                for row in rows
                if row.get("prefixes_before") is not None and row.get("prefixes_after") is not None
            ]
            if (
                not events.success
                or events.data.get("status") != "ok"
                or not (any(n < 0 for n in changes) and any(n > 0 for n in changes))
            ):
                errors.append("Repeated actual route withdrawals and advertisements were not observed")
            session = toolkit.get_bgp_neighbor(root, p["peer_ip"])
            evidence["flapping_session"] = session.to_dict()
            if not session.success or session.data.get("state") != "Established":
                errors.append("Route flapping did not preserve the BGP session")
    if fault.startswith("acl_"):
        receipt = injection.get("evidence", {})
        rule = receipt.get("rule", [])
        counter = executor.injector._cmd.docker_exec(
            executor.injector.container_names[root], ["iptables", "-L", "FORWARD", "-n", "-v", "-x"]
        )
        evidence["counters"] = counter.stdout
        line = next((line for line in counter.stdout.splitlines() if receipt.get("acl_name", "!") in line), "")
        hit = line.split()[0] if line else ""
        if counter.returncode or not hit.isdigit() or int(hit) <= 0:
            errors.append("Owned ACL rule has no verified hits")
        if not rule or ("-i" not in rule and "-o" not in rule):
            errors.append("ACL interface/direction was not applied")
        if fault == "acl_l4_port_misconfig":
            pair = p.get("affected_candidates", [{}])[0]
            if pair.get("src"):
                icmp = toolkit.ping_test(pair["src"], executor.current_manifest().device(pair["dst"]).data_ip, count=3)
                evidence["icmp_control"] = icmp.to_dict()
                if not icmp.success or icmp.data.get("return_code"):
                    errors.append("ICMP control is not reachable")
        # The immutable ECMP tuples can send regular and DF probes over
        # different members. DF loss proves ACL impact only when no local
        # send error or MTU-drop confirmation explains it.
        network_df_rows = [
            row
            for row in affected_rows
            if not row.get("fault", {}).get("local_probe_errors")
            and not row.get("fault", {}).get("df", {}).get("mtu_drops")
        ]
        evidence["matched_probe_loss_kinds"] = [
            kind for kind, rows in (("regular", affected_rows), ("df", network_df_rows)) if _measured_loss(rows, kind)
        ]
        if not evidence["matched_probe_loss_kinds"]:
            errors.append("ACL rule produced no observed probe loss")
    if fault in {"blackhole_route", "static_route_misconfig"} and not _measured_loss(affected_rows):
        errors.append("Static route had no observed effect")
    evidence["pingmesh_quality"] = quality
    observable = set(unobservable)
    anomalies = episode_result.get("observations", {}).get("pingmesh_metrics", {}).get("anomalies", [])
    for pair in p.get("control_candidates", []):
        if pair["src"] in observable or pair["dst"] in observable:
            continue
        row = by_pair.get((pair["src"], pair["dst"]), {})
        if not row.get("fault", {}).get("samples"):
            errors.append(f"Control flow has no fault-window evidence: {pair}")
        if not validation_only and any(
            item.get("src_name") == pair["src"] and item.get("dst_name") == pair["dst"] for item in anomalies
        ):
            errors.append(f"Control flow is anomalous: {pair}")
    return {
        "valid": not errors,
        "errors": errors,
        "evidence": evidence,
        "unobservable_clients": sorted(set(unobservable)),
        "bounded_client_outages": bounded_outages,
        "probe_cycle_seconds": executor.current_manifest().require_pingmesh_plan().settings.cycle_interval_seconds,
    }


def scoped_integrity_errors(observations, audit, effect):
    """Explain only proven endpoint outages; retain the original full audit."""
    from .observation import observation_integrity_errors

    excluded = set(effect.get("unobservable_clients", []))
    outages = effect.get("bounded_client_outages", [])
    if not excluded and not outages:
        return observation_integrity_errors(observations)
    errors = []
    if observations.get("data_source_status") != "ok":
        errors.append("Observation ingestion/query failure")
    if (observations.get("_baseline_coverage") or {}).get("coverage_status") != "complete":
        errors.append("Healthy baseline coverage is incomplete")
    if audit.get("unexpected_rows"):
        errors.append("Unexpected peer/frame records remain")
    if "missing_tasks" not in audit or "invalid_tasks" not in audit:
        errors.append("Detailed coverage audit unavailable")
    if outages:
        explained_tasks = set()
        rows = audit.get("invalid_rows", [])
        if len(rows) != audit.get("invalid_socket_rows"):
            errors.append("Timed invalid-row audit unavailable")
        for row in rows:
            if not _outage_explains_local_error(row, outages, effect["probe_cycle_seconds"]):
                errors.append(f"Unexplained local probe error {row}")
            else:
                explained_tasks.add((row["src"], row["dst"], row.get("port_batch_index")))
        errors.extend(
            f"Unobserved periodic probe task {task}"
            for task in audit.get("missing_tasks", [])
            if tuple(task) not in explained_tasks
        )
        if not audit.get("observable_pairs"):
            errors.append("No remaining observable probe pairs")
        return errors
    rows = audit.get("invalid_rows", [])
    if len(rows) != audit.get("invalid_socket_rows", 0):
        errors.append("Detailed invalid-row audit unavailable")
    for row in rows:
        if row.get("socket_batch_valid") is not True:
            errors.append(f"Incorrect port batch {row}")
    for task in audit.get("missing_tasks", []) + audit.get("invalid_tasks", []):
        if task[0] not in excluded and task[1] not in excluded:
            errors.append(f"Unexplained probe task {task}")
    if not any(src not in excluded and dst not in excluded for src, dst in audit.get("observable_pairs", [])):
        errors.append("No remaining observable probe pairs")
    return errors

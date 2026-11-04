"""Scoped configuration faults with an undo journal and independent policies.

All preconditions are read before mutation. Every attempted write is journaled
before execution, so partial CLI failures still participate in compensation.
"""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from pathlib import Path

from ..services.periodic import PeriodicController

_BGPD_RESTORE_SOURCE = (Path(__file__).parent.parent / "services" / "_bgpd_restore.py").read_text(encoding="utf-8")


class NetworkFaultHandler:
    def __init__(self, cmd, sonic, iface, routing, tracker, ctx):
        self.cmd, self.sonic, self.iface = cmd, sonic, iface
        self.routing, self.tracker, self.ctx = routing, tracker, ctx

    def _run(self, device, kind, commands):
        if kind == "frr":
            result = self.sonic.vtysh(device, commands)
        else:
            result = self.cmd.docker_exec(self.ctx.container_names[device], commands)
        service_status = commands[:2] == ["supervisorctl", "status"] and result.returncode == 3
        if (result.returncode and not service_status) or (kind == "frr" and re.search(r"(?m)^%", result.stdout or "")):
            raise RuntimeError(result.stderr or result.stdout or f"{kind} command failed")
        return result.stdout or ""

    def _json(self, device, commands):
        return json.loads(self._run(device, "docker", commands))

    def _frr_json(self, device, command):
        return json.loads(self._run(device, "frr", [command]))

    @staticmethod
    def _config(commands):
        return ["configure terminal", *commands, "end", "write memory"]

    def _operation(self, kind, apply, restore):
        return {"kind": kind, "apply": apply, "restore": restore}

    def _frr_operation(self, apply, restore):
        return self._operation("frr", self._config(apply), self._config(restore))

    def _neighbor(self, device, peer):
        neighbor = self.routing.pick_bgp_neighbor(device, peer_ip=peer)
        if not neighbor or not self.sonic.bgp_neighbor_state(device, peer):
            raise RuntimeError(f"Target neighbor is not readable and established: {device}/{peer}")
        return neighbor

    def _prefix_policy(self, device, prefix, peers, direction, *, network=None):
        config = self._run(device, "frr", ["show running-config"])
        # Preserve other policy terms by cloning an existing attachment, placing
        # the exact-prefix deny before them. Never modify the shared original.
        token = "P" + uuid.uuid4().hex[:12].upper()
        operations = []
        attachments = [(peer, None) for peer in peers] if network is None else [(None, network)]
        snapshot = self.routing.get_bgp_config_snapshot(device)
        asn = self.routing.get_device_asn(device)
        for index, (peer, origin) in enumerate(attachments):
            route_map = f"{token}{index}"
            prefix_list = route_map + "L"
            if origin is not None:
                original = origin.get("route_map")
            else:
                neighbor = next(n for n in snapshot["neighbors"] if n["peer_ip"] == peer)
                original = next((a["name"] for a in neighbor["route_maps"] if a["direction"] == direction), None)
            cloned = []
            if original:
                blocks = re.findall(
                    rf"(?m)^route-map {re.escape(original)} (?:permit|deny) \d+\n(?:(?: .*)?\n)*", config + "\n"
                )
                if not blocks:
                    raise RuntimeError(f"Cannot snapshot attached policy {original}")
                for block in blocks:
                    lines = block.rstrip().splitlines()
                    head = lines[0].split()
                    sequence = int(head[-1])
                    if sequence <= 1:
                        raise RuntimeError("No safe sequence before the existing route-map")
                    cloned.extend([lines[0].replace(original, route_map, 1), *[line.strip() for line in lines[1:]]])
            else:
                cloned = [f"route-map {route_map} permit 100"]
            apply = [
                f"ip prefix-list {prefix_list} seq 5 permit {prefix}",
                f"route-map {route_map} deny 1",
                f"match ip address prefix-list {prefix_list}",
                *cloned,
                f"router bgp {asn}",
                "address-family ipv4 unicast",
            ]
            restore = [f"router bgp {asn}", "address-family ipv4 unicast"]
            if origin is not None:
                statement = self.routing.format_network_statement(prefix, original)
                apply += [f"no {statement}", f"network {prefix} route-map {route_map}"]
                restore += [f"no network {prefix} route-map {route_map}", statement]
            else:
                apply += [f"neighbor {peer} route-map {route_map} {direction}"]
                restore += [
                    (
                        f"neighbor {peer} route-map {original} {direction}"
                        if original
                        else f"no neighbor {peer} route-map {route_map} {direction}"
                    )
                ]
            restore += ["exit-address-family", "exit", f"no route-map {route_map}", f"no ip prefix-list {prefix_list}"]
            operation = self._frr_operation(apply, restore)
            if peer is not None:
                refresh = f"clear bgp ipv4 unicast {peer} soft {direction}"
                operation["apply"].append(refresh)
                operation["restore"].append(refresh)
            operations.append(operation)
        return operations

    def _prepare(self, episode):
        device, fault, p = episode.target_device, episode.fault_type, dict(episode.parameters)
        prefix = episode.target_prefix
        container = self.ctx.container_names[device]
        asn = self.routing.get_device_asn(device)
        config = (
            self._run(device, "frr", ["show running-config"])
            if fault
            not in {
                "interface_ip_misconfig",
                "bgp_process_down",
                "acl_source_misconfig",
                "acl_destination_misconfig",
                "acl_l4_port_misconfig",
            }
            else ""
        )
        evidence = {}
        if fault.startswith("bgp_") and fault != "bgp_process_down":
            peer = str(ipaddress.ip_address(p["peer_ip"]))
            neighbor = self._neighbor(device, peer)
            keyword, value = {
                "bgp_as_mismatch": ("remote-as", str(int(neighbor["remote_as"]) + 1000)),
                "bgp_auth_mismatch": ("password", uuid.uuid4().hex),
                "bgp_update_source_mismatch": ("update-source", p.get("fault_update_source", "Loopback999")),
                "bgp_neighbor_shutdown": ("shutdown", ""),
                "bgp_max_prefix_misconfig": ("maximum-prefix", ""),
            }[fault]
            original = re.search(rf"(?m)^\s*neighbor {re.escape(peer)} {keyword}(?: (.*))?$", config)
            if fault == "bgp_update_source_mismatch":
                # FRR permits a passive connection despite an incorrect source
                # IP. An absent source interface also makes incoming next-hop
                # validation fail, while changing only this neighbor's option.
                if not isinstance(value, str) or not re.fullmatch(r"Loopback[0-9]{1,4}", value):
                    raise ValueError("Invalid update-source interface")
                links = self._json(device, ["ip", "-j", "link", "show"])
                if any(link["ifname"] == value for link in links):
                    raise RuntimeError("Reserved wrong update-source interface already exists")
                evidence["fault_update_source"] = value
            if fault == "bgp_max_prefix_misconfig":
                detail = self._frr_json(device, f"show ip bgp neighbors {peer} json").get(peer, {})
                received = int(
                    detail.get("addressFamilyInfo", {}).get("ipv4Unicast", {}).get("acceptedPrefixCounter", 0)
                )
                if received < 2:
                    raise RuntimeError("maximum-prefix requires at least two healthy received prefixes")
                value = str(received - 1)
                evidence["healthy_prefix_count"] = received
            stem = f"neighbor {peer} {keyword}"
            apply = [f"router bgp {asn}"]
            restore = [f"router bgp {asn}"]
            if keyword == "maximum-prefix":
                apply += ["address-family ipv4 unicast"]
                restore += ["address-family ipv4 unicast"]
            apply.append(f"{stem} {value}".rstrip())
            restore.append(original.group(0).strip() if original else f"no {stem}")
            operation = self._frr_operation(apply, restore)
            if keyword == "maximum-prefix":
                operation["apply"].append(f"clear bgp ipv4 unicast {peer} soft in")
                operation["restore"].append(f"clear bgp ipv4 unicast {peer}")
            return [operation], evidence
        if fault == "bgp_process_down":
            status = self._run(device, "docker", ["supervisorctl", "status", "bgpd"])
            if "RUNNING" not in status:
                raise RuntimeError("bgpd must be running before injection")
            config = self._run(device, "docker", ["vtysh", "-d", "bgpd", "-c", "show running-config"])
            _, marker, original = config.partition("Current configuration:\n")
            if not marker or not re.search(r"(?m)^router bgp \d+", original):
                raise RuntimeError("Cannot capture the running bgpd configuration before stopping it")
            evidence.update(original_service=status.strip(), original_bgp_config=original)
            return [
                self._operation(
                    "docker",
                    ["supervisorctl", "stop", "bgpd"],
                    ["python3", "-c", _BGPD_RESTORE_SOURCE, original],
                )
            ], evidence
        if fault == "interface_ip_misconfig":
            interface = self.iface.resolve_sonic(episode.target_interface)
            addresses = self._json(device, ["ip", "-j", "-4", "addr", "show", "dev", interface])
            original = [
                f"{a['local']}/{a['prefixlen']}"
                for a in addresses[0].get("addr_info", [])
                if a.get("scope") == "global"
            ]
            if len(original) != 1 or "UP" not in addresses[0].get("flags", []):
                raise RuntimeError("Address fault requires one address on an admin-up data interface")
            configured = self._run(
                device, "docker", ["sonic-db-cli", "CONFIG_DB", "exists", f"INTERFACE|{interface}|{original[0]}"]
            )
            if configured.strip() != "1":
                raise RuntimeError("SONiC and live interface addresses disagree")
            wrong = ipaddress.ip_interface(p.get("fault_address", "198.51.100.1/30"))
            networks = [ipaddress.ip_network(self.ctx.manifest.management.ipv4_subnet)]
            networks += [ipaddress.ip_network(c.data_ip + "/30", strict=False) for c in self.ctx.manifest.clients()]
            for switch in self.ctx.manifest.switches():
                config_path = self.ctx.topology_dir / "configs" / "sonic" / switch.name / "config_db.json"
                config_db = json.loads(config_path.read_text())
                networks += [
                    ipaddress.ip_network(key.split("|", 1)[1], strict=False)
                    for key in config_db["INTERFACE"]
                    if "|" in key
                ]
            if wrong.ip not in ipaddress.ip_network("198.51.100.0/24") or any(
                wrong.network.overlaps(net) for net in networks
            ):
                raise ValueError("Fault address must use the reserved, non-overlapping pool")
            operations = [
                # Keep the routed interface present throughout replacement.
                # The last-address removal path also shells out through sudo,
                # which the pinned root-only SONiC image does not provide.
                self._operation(
                    "docker",
                    ["config", "interface", "ip", "add", interface, str(wrong)],
                    ["config", "interface", "ip", "remove", interface, str(wrong)],
                ),
                self._operation(
                    "docker",
                    ["config", "interface", "ip", "remove", interface, original[0]],
                    ["config", "interface", "ip", "add", interface, original[0]],
                ),
            ]
            evidence.update(original_address=original[0], fault_address=str(wrong))
            return operations, evidence
        if fault in {"route_origination_misconfig", "route_filter_misconfig", "route_flapping"}:
            mechanism = p.get("mechanism")
            if fault == "route_origination_misconfig":
                network = self.routing.pick_advertised_network(device, prefix)
                if not network:
                    raise RuntimeError("Prefix is not originated on the root device")
                if mechanism == "network_statement_missing":
                    statement = self.routing.format_network_statement(prefix, network.get("route_map"))
                    return [
                        self._frr_operation(
                            [f"router bgp {asn}", "address-family ipv4 unicast", f"no {statement}"],
                            [f"router bgp {asn}", "address-family ipv4 unicast", statement],
                        )
                    ], evidence
                return self._prefix_policy(device, prefix, [], "out", network=network), evidence
            peers = p.get("peer_ips", [p.get("peer_ip")])
            if not peers or any(not peer for peer in peers):
                raise ValueError("Prefix filter requires explicit peers")
            peers = [str(ipaddress.ip_address(peer)) for peer in peers]
            for peer in peers:
                self._neighbor(device, peer)
            direction = "in" if mechanism == "import_filter" else "out"
            return self._prefix_policy(device, prefix, peers, direction), evidence
        if fault == "ecmp_misconfig":
            routes = self._json(device, ["ip", "-j", "-4", "route", "show", "exact", prefix])
            hops = [hop for route in routes for hop in route.get("nexthops", [])]
            if len(hops) < 2:
                raise RuntimeError("ECMP requires multiple actual kernel FIB next hops")
            evidence["healthy_fib"] = routes
            rib = self._frr_json(device, f"show bgp ipv4 unicast {prefix} json")
            evidence["healthy_bgp"] = rib
            mechanism = p["mechanism"]
            if mechanism == "multipath_relax_disabled":
                gateways = {hop.get("gateway") for hop in hops if hop.get("gateway")}
                as_paths = set()
                for path in rib.get("paths", []):
                    if not path.get("valid") or not (path.get("multipath") or path.get("bestpath", {}).get("overall")):
                        continue
                    if not any(
                        hop.get("accessible") and hop.get("used") and hop.get("ip") in gateways
                        for hop in path.get("nexthops", [])
                    ):
                        continue
                    as_path = path.get("aspath", {}).get("string")
                    if isinstance(as_path, str) and as_path.strip():
                        as_paths.add(tuple(as_path.split()))
                if len(as_paths) < 2:
                    raise RuntimeError("multipath-relax requires distinct valid AS_PATHs")
                if "bgp bestpath as-path multipath-relax" not in config:
                    raise RuntimeError("multipath-relax is already disabled")
                return [
                    self._frr_operation(
                        [f"router bgp {asn}", "no bgp bestpath as-path multipath-relax"],
                        [f"router bgp {asn}", "bgp bestpath as-path multipath-relax"],
                    )
                ], evidence
            original = re.search(r"(?m)^\s*maximum-paths (\d+)\s*$", config)
            apply = [f"router bgp {asn}", "address-family ipv4 unicast", "maximum-paths 1"]
            restore = [
                f"router bgp {asn}",
                "address-family ipv4 unicast",
                original.group(0).strip() if original else "no maximum-paths 1",
            ]
            return [self._frr_operation(apply, restore)], evidence
        if fault == "routing_loop":
            nexthop = str(ipaddress.ip_address(p["next_hop"]))
            self._neighbor(device, nexthop)
            if any(line.strip().startswith(f"ip route {prefix} ") for line in config.splitlines()):
                raise RuntimeError("Target static route already exists; refusing to overwrite it")
            statement = f"ip route {prefix} {nexthop}"
            return [self._frr_operation([statement], [f"no {statement}"])], evidence
        if fault.startswith("acl_"):
            interface = self.iface.resolve_sonic(episode.target_interface)
            direction = p.get("direction", "in")
            linux = self.iface.resolve_linux(interface)
            # SONiC virtual dataplane interfaces use Ethernet names after startup.
            live = self.cmd.docker_exec(container, ["ip", "link", "show", "dev", interface])
            if live.returncode == 0:
                linux = interface
            name = "P" + uuid.uuid4().hex[:12].upper()
            match = {
                "acl_source_misconfig": ["-s", p.get("source_prefix", prefix)],
                "acl_destination_misconfig": ["-d", prefix],
                "acl_l4_port_misconfig": ["-p", "udp", "--dport", str(p.get("destination_port", 33434))],
            }[fault]
            rule = [
                "FORWARD",
                "-i" if direction == "in" else "-o",
                linux,
                *match,
                "-m",
                "comment",
                "--comment",
                name,
                "-j",
                "DROP",
            ]
            table = f"ACL_TABLE|{name}"
            key = f"ACL_RULE|{name}|RULE1"
            fields = {"PRIORITY": "999", "PACKET_ACTION": "DROP"}
            if fault == "acl_source_misconfig":
                fields["SRC_IP"] = match[1]
            elif fault == "acl_destination_misconfig":
                fields["DST_IP"] = prefix
            else:
                fields.update(IP_PROTOCOL="17", L4_DST_PORT=match[-1])
            operations = [
                self._operation(
                    "docker",
                    [
                        "sonic-db-cli",
                        "CONFIG_DB",
                        "hset",
                        table,
                        "type",
                        "L3",
                        "ports@",
                        interface,
                        "stage",
                        "INGRESS" if direction == "in" else "EGRESS",
                        "policy_desc",
                        name,
                    ],
                    ["sonic-db-cli", "CONFIG_DB", "del", table],
                ),
                self._operation(
                    "docker",
                    ["sonic-db-cli", "CONFIG_DB", "hset", key, *[value for pair in fields.items() for value in pair]],
                    ["sonic-db-cli", "CONFIG_DB", "del", key],
                ),
                self._operation("docker", ["iptables", "-I", *rule], ["iptables", "-D", *rule]),
            ]
            evidence.update(acl_name=name, rule=rule, fields=fields, direction=direction, interface=interface)
            return operations, evidence
        raise ValueError(f"No scoped handler for {fault}")

    def _verify_written(self, episode, info):
        device, fault = episode.target_device, episode.fault_type
        if fault == "bgp_process_down":
            if "STOPPED" not in self._run(device, "docker", ["supervisorctl", "status", "bgpd"]):
                raise RuntimeError("Supervisor did not stop bgpd")
            return
        if fault == "interface_ip_misconfig":
            interface = self.iface.resolve_sonic(episode.target_interface)
            rows = self._json(device, ["ip", "-j", "-4", "addr", "show", "dev", interface])
            addresses = {f"{a['local']}/{a['prefixlen']}" for row in rows for a in row.get("addr_info", [])}
            if info["evidence"]["fault_address"] not in addresses or info["evidence"]["original_address"] in addresses:
                raise RuntimeError("Live address readback disagrees with the injected configuration")
            if "UP" not in rows[0].get("flags", []):
                raise RuntimeError("Address mutation changed admin state")
            return
        if fault.startswith("acl_"):
            evidence = info["evidence"]
            self._run(device, "docker", ["iptables", "-C", *evidence["rule"]])
            for key in (f"ACL_TABLE|{evidence['acl_name']}", f"ACL_RULE|{evidence['acl_name']}|RULE1"):
                if self._run(device, "docker", ["sonic-db-cli", "CONFIG_DB", "exists", key]).strip() != "1":
                    raise RuntimeError("ACL ConfigDB readback is missing")
            return
        config = {" ".join(line.split()) for line in self._run(device, "frr", ["show running-config"]).splitlines()}
        contexts = ("router bgp ", "address-family ", "match ", "set ")
        for operation in info["journal"]:
            for command in operation["apply"]:
                if command in {"configure terminal", "end", "write memory"} or command.startswith(
                    (*contexts, "clear ")
                ):
                    continue
                normalized = " ".join(command.split())
                if command.startswith("no "):
                    if normalized[3:] in config:
                        raise RuntimeError(f"Configuration remained present: {normalized[3:]}")
                elif normalized not in config:
                    raise RuntimeError(f"Configuration readback is missing: {normalized}")

    def inject(self, episode):
        info = {
            "fault_id": uuid.uuid4().hex,
            "type": episode.fault_type,
            "device": episode.target_device,
            "interface": episode.target_interface,
            "prefix": episode.target_prefix,
            "mechanism": episode.parameters.get("mechanism", episode.fault_type),
            "success": False,
            "error": None,
            "journal": [],
        }
        try:
            operations, evidence = self._prepare(episode)
            info["evidence"] = evidence
            if any(operation["kind"] == "frr" for operation in operations):
                from netopsbench.platform.scenario.baseline import normalized_frr

                evidence["original_frr_digest"] = normalized_frr(
                    self._run(info["device"], "frr", ["show running-config"])
                )
            if episode.fault_type.startswith("acl_"):
                evidence["original_iptables"] = self._run(info["device"], "docker", ["iptables", "-S"])
                for key in (f"ACL_TABLE|{evidence['acl_name']}", f"ACL_RULE|{evidence['acl_name']}|RULE1"):
                    if self._run(info["device"], "docker", ["sonic-db-cli", "CONFIG_DB", "exists", key]).strip() != "0":
                        raise RuntimeError("Cannot own an existing ACL object")
            for operation in operations:
                info["journal"].append(operation)
                self._run(info["device"], operation["kind"], operation["apply"])
            self._verify_written(episode, info)
            if episode.fault_type == "route_flapping":

                def apply():
                    for operation in operations:
                        self._run(info["device"], operation["kind"], operation["apply"])

                def restore():
                    for operation in reversed(operations):
                        self._run(info["device"], operation["kind"], operation["restore"])

                controller = PeriodicController(
                    apply,
                    restore,
                    down_time=episode.parameters.get("down_time", self.ctx.experiment.flapping_down_seconds),
                    up_time=episode.parameters.get("up_time", self.ctx.experiment.flapping_up_seconds),
                )
                # Already in the first fault phase; start the waiting loop only.
                control_id = uuid.uuid4().hex
                self.tracker.register_background_control(
                    control_id, stop_event=controller.stop_event, thread=controller.thread, controller=controller
                )
                controller.thread.start()
                info["control_id"] = control_id
            info["success"] = True
            self.tracker.track(info)
        except Exception as exc:
            info["error"] = str(exc)
            recovery = self.recover(info)
            if not recovery["recovered"]:
                info["error"] += "; compensation: " + str(recovery["error"])
                self.tracker.track_residual(info, info["error"])
        return info

    def _verify_restored(self, fault):
        """Read actual owned resources after undo; never infer success from exit code."""
        from netopsbench.platform.scenario.baseline import normalized_frr

        device, evidence = fault["device"], fault.get("evidence", {})
        if evidence.get("original_frr_digest"):
            if normalized_frr(self._run(device, "frr", ["show running-config"])) != evidence["original_frr_digest"]:
                raise RuntimeError("FRR configuration was not restored")
            return True
        if "original_bgp_config" in evidence:
            if "RUNNING" not in self._run(device, "docker", ["supervisorctl", "status", "bgpd"]):
                raise RuntimeError("bgpd is not running")
            current = self._run(device, "docker", ["vtysh", "-d", "bgpd", "-c", "show running-config"])
            if normalized_frr(current) != normalized_frr(evidence["original_bgp_config"]):
                raise RuntimeError("bgpd configuration was not restored")
            return True
        if "original_address" in evidence:
            interface = self.iface.resolve_sonic(fault["interface"])
            rows = self._json(device, ["ip", "-j", "-4", "addr", "show", "dev", interface])
            actual = {
                f"{a['local']}/{a['prefixlen']}"
                for row in rows
                for a in row.get("addr_info", [])
                if a.get("scope") == "global"
            }
            if actual != {evidence["original_address"]}:
                raise RuntimeError("Interface addresses were not restored")
            for address, wanted in [(evidence["original_address"], "1"), (evidence["fault_address"], "0")]:
                key = f"INTERFACE|{interface}|{address}"
                if self._run(device, "docker", ["sonic-db-cli", "CONFIG_DB", "exists", key]).strip() != wanted:
                    raise RuntimeError("Interface CONFIG_DB was not restored")
            return True
        if "original_iptables" in evidence:
            if self._run(device, "docker", ["iptables", "-S"]) != evidence["original_iptables"]:
                raise RuntimeError("Actual ACL rules were not restored")
            for key in (f"ACL_TABLE|{evidence['acl_name']}", f"ACL_RULE|{evidence['acl_name']}|RULE1"):
                if self._run(device, "docker", ["sonic-db-cli", "CONFIG_DB", "exists", key]).strip() != "0":
                    raise RuntimeError("Owned ACL configuration remains")
            return True
        return False  # Historical/custom records have no independently captured readback evidence.

    def recover(self, fault):
        errors = []
        control = fault.get("control_id")
        if control and not self.tracker.stop_background(control, join_timeout=65):
            return {"type": fault["type"], "recovered": False, "error": "Periodic controller still running"}
        for operation in reversed(fault.get("journal", [])):
            try:
                # iptables -C distinguishes an already absent owned rule from
                # permission/tool failures; missing objects are idempotent.
                commands = operation["restore"]
                if operation["kind"] == "frr":
                    current = {
                        " ".join(line.split())
                        for line in self._run(fault["device"], "frr", ["show running-config"]).splitlines()
                    }

                    def present(statement, current=current):
                        parameter_stem = statement.startswith("neighbor ") and statement.split()[-1] in {
                            "password",
                            "remote-as",
                            "update-source",
                            "maximum-prefix",
                        }
                        if parameter_stem or statement.startswith(("route-map ", "ip prefix-list ")):
                            return any(line == statement or line.startswith(statement + " ") for line in current)
                        return statement in current

                    commands = [
                        command
                        for command in commands
                        if not command.startswith("no ") or present(" ".join(command[3:].split()))
                    ]
                if commands[:4] == ["config", "interface", "ip", "remove"]:
                    interface, address = commands[4:]
                    live = self._json(fault["device"], ["ip", "-j", "-4", "addr", "show", "dev", interface])
                    addresses = {f"{a['local']}/{a['prefixlen']}" for row in live for a in row.get("addr_info", [])}
                    key = f"INTERFACE|{interface}|{address}"
                    configured = self._run(
                        fault["device"], "docker", ["sonic-db-cli", "CONFIG_DB", "exists", key]
                    ).strip()
                    if configured == "0" and address not in addresses:
                        continue
                    if configured not in {"0", "1"}:
                        raise RuntimeError("Cannot read owned interface address state")
                if commands[:2] == ["iptables", "-D"]:
                    check = self.cmd.docker_exec(
                        self.ctx.container_names[fault["device"]], ["iptables", "-C", *commands[2:]]
                    )
                    if check.returncode == 1:
                        continue
                    if check.returncode:
                        raise RuntimeError(check.stderr or "ACL presence check failed")
                self._run(fault["device"], operation["kind"], commands)
            except Exception as exc:
                errors.append(str(exc))
        verified = False
        if not errors and fault.get("journal"):
            try:
                verified = self._verify_restored(fault)
            except Exception as exc:
                errors.append(str(exc))
        if not errors:
            owner = fault.get("fault_id")
            if owner:
                self.tracker.remove_faults(lambda f: f.get("fault_id") == owner)
        return {
            "type": fault["type"],
            "device": fault["device"],
            "recovered": not errors,
            "readback_verified": verified and not errors,
            "error": "; ".join(errors) if errors else None,
        }

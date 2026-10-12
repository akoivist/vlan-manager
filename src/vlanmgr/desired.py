"""Validation and intent compilation for desired switch state."""

from __future__ import annotations

import re
from typing import Any

from vlanmgr.adapters.tplink import SUPPORTED_MODELS


_TOP_LEVEL_KEYS = {"version", "router", "vlans", "wifi_networks", "switches"}
_VLAN_ALIAS = re.compile(r"[A-Za-z0-9_-]+")
_INTENT = re.compile(r"dhcp:([A-Za-z0-9_-]+)")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must be a mapping")
    return value


def _vlan_alias(reference: Any, vlans: dict[str, Any], path: str) -> str:
    if not isinstance(reference, str) or reference not in vlans:
        raise RuntimeError(f"{path} references unknown VLAN {reference!r}")
    return reference


def _port_vlan_alias(reference: Any, vlans: dict[str, Any], path: str) -> str:
    alias = _vlan_alias(reference, vlans, path)
    if alias == "parking":
        raise RuntimeError(
            f"{path} must not reference parking; parking is reserved for fallback PVIDs"
        )
    return alias


def _vlan_indexes(config: dict[str, Any]) -> tuple[dict[str, int], dict[int, str]]:
    aliases_to_ids = {
        alias: details["id"] for alias, details in config["vlans"].items()
    }
    ids_to_names = {
        details["id"]: details["name"] for details in config["vlans"].values()
    }
    return aliases_to_ids, ids_to_names


def _validate_vlan_sections(config: dict[str, Any]) -> None:
    vlans = _mapping(config.get("vlans"), "vlans")
    if not vlans:
        raise RuntimeError("vlans must include VLAN 1 and the parking VLAN")

    seen_ids: dict[int, str] = {}
    seen_names: dict[str, str] = {}
    for alias, raw_details in vlans.items():
        if not isinstance(alias, str) or not _VLAN_ALIAS.fullmatch(alias):
            raise RuntimeError(f"Invalid VLAN alias {alias!r}")
        details = _mapping(raw_details, f"vlans.{alias}")
        unexpected = set(details) - {"id", "name"}
        if unexpected:
            raise RuntimeError(
                f"vlans.{alias} has unsupported keys: "
                f"{', '.join(sorted(map(str, unexpected)))}"
            )
        vid = details.get("id")
        if isinstance(vid, bool) or not isinstance(vid, int) or not 1 <= vid <= 4094:
            raise RuntimeError(f"vlans.{alias}.id must be an integer from 1 to 4094")
        vlan_name = details.get("name")
        if not isinstance(vlan_name, str) or not vlan_name.strip():
            raise RuntimeError(f"vlans.{alias}.name must be a non-empty string")
        normalized_name = vlan_name.casefold()
        if normalized_name in seen_names:
            raise RuntimeError(
                f"VLAN name {vlan_name!r} is shared by aliases "
                f"{seen_names[normalized_name]!r} and {alias!r}"
            )
        if vid in seen_ids:
            raise RuntimeError(
                f"VLAN ID {vid} is assigned to both {seen_ids[vid]!r} and {alias!r}"
            )
        seen_ids[vid] = alias
        seen_names[normalized_name] = alias

    if seen_ids.get(1) is None:
        raise RuntimeError("VLAN ID 1 must be declared")
    if "parking" not in vlans:
        raise RuntimeError("vlans.parking is required as the fallback PVID VLAN")

    wifi_networks = _mapping(config.get("wifi_networks", {}), "wifi_networks")
    for network, raw_details in wifi_networks.items():
        if not isinstance(network, str) or not network.strip():
            raise RuntimeError(f"Invalid Wi-Fi network name {network!r}")
        details = _mapping(raw_details, f"wifi_networks.{network}")
        unexpected = set(details) - {"ssid", "vlan"}
        if unexpected:
            raise RuntimeError(
                f"wifi_networks.{network} has unsupported keys: "
                f"{', '.join(sorted(map(str, unexpected)))}"
            )
        ssid = details.get("ssid")
        if not isinstance(ssid, str) or not ssid.strip():
            raise RuntimeError(
                f"wifi_networks.{network}.ssid must be a non-empty string"
            )
        _port_vlan_alias(
            details.get("vlan"), vlans, f"wifi_networks.{network}.vlan"
        )


def _validate_switches(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    switches = _mapping(config.get("switches"), "switches")
    if not switches:
        raise RuntimeError("switches must not be empty")
    router_name = config["router"]["name"]
    if router_name in switches:
        raise RuntimeError("router.name must not also be a switch name")

    vlans = config["vlans"]
    wifi_networks = config.get("wifi_networks", {})
    for switch_name, raw_switch in switches.items():
        if not isinstance(switch_name, str) or not switch_name.strip():
            raise RuntimeError(f"Invalid switch name {switch_name!r}")
        switch = _mapping(raw_switch, f"switches.{switch_name}")
        unexpected = set(switch) - {"host", "model", "port_count", "ports"}
        if unexpected:
            raise RuntimeError(
                f"switches.{switch_name} has unsupported keys: "
                f"{', '.join(sorted(map(str, unexpected)))}"
            )
        host = switch.get("host")
        if not isinstance(host, str) or not host.strip():
            raise RuntimeError(
                f"switches.{switch_name}.host must be a non-empty string"
            )
        model = switch.get("model")
        if not isinstance(model, str) or model not in SUPPORTED_MODELS:
            raise RuntimeError(
                f"switches.{switch_name}.model {model!r} is unsupported or unverified"
            )
        port_count = switch.get("port_count")
        if (
            isinstance(port_count, bool)
            or not isinstance(port_count, int)
            or port_count != SUPPORTED_MODELS[model]
        ):
            raise RuntimeError(
                f"switches.{switch_name}.port_count must be "
                f"{SUPPORTED_MODELS[model]} for {model}"
            )
        _validate_ports(switch_name, switch, port_count, vlans, wifi_networks)
    return switches


def _validate_ports(
    switch_name: str,
    switch: dict[str, Any],
    port_count: int,
    vlans: dict[str, Any],
    wifi_networks: dict[str, Any],
) -> None:
    ports = _mapping(switch.get("ports", {}), f"switches.{switch_name}.ports")
    for raw_number, raw_port in ports.items():
        if isinstance(raw_number, bool) or not isinstance(raw_number, int):
            raise RuntimeError(
                f"switches.{switch_name}.ports keys must be integer port numbers"
            )
        if not 1 <= raw_number <= port_count:
            raise RuntimeError(
                f"switches.{switch_name}.ports declares invalid port {raw_number}"
            )
        path = f"switches.{switch_name}.ports.{raw_number}"
        port = _mapping(raw_port, path)
        kind = port.get("kind")
        if not isinstance(kind, str) or kind not in {"access", "trunk", "wifi", "link"}:
            raise RuntimeError(
                f"{path}.kind must be access, trunk, wifi, or link"
            )
        if "name" in port and (
            not isinstance(port["name"], str) or not port["name"].strip()
        ):
            raise RuntimeError(f"{path}.name must be a non-empty string")

        if kind == "access":
            _port_vlan_alias(port.get("native_vlan"), vlans, f"{path}.native_vlan")
            allowed = {"kind", "name", "native_vlan"}
        elif kind == "trunk":
            trunk_vlans = port.get("vlans")
            if not isinstance(trunk_vlans, list) or not trunk_vlans:
                raise RuntimeError(f"{path}.vlans must be a non-empty list for a trunk")
            for index, alias in enumerate(trunk_vlans):
                _port_vlan_alias(alias, vlans, f"{path}.vlans[{index}]")
            if len(set(trunk_vlans)) != len(trunk_vlans):
                raise RuntimeError(f"{path}.vlans contains duplicate VLAN aliases")
            if "native_vlan" in port:
                _port_vlan_alias(port["native_vlan"], vlans, f"{path}.native_vlan")
            allowed = {"kind", "name", "vlans", "native_vlan"}
        elif kind == "wifi":
            if not wifi_networks:
                raise RuntimeError(f"{path} is a Wi-Fi port but wifi_networks is empty")
            _port_vlan_alias(port.get("native_vlan"), vlans, f"{path}.native_vlan")
            allowed = {"kind", "name", "native_vlan"}
        else:
            has_upstream = "upstream" in port
            has_downstream = "downstream" in port
            if has_upstream == has_downstream:
                raise RuntimeError(
                    f"{path} must declare exactly one of upstream or downstream"
                )
            peer = port.get("upstream") if has_upstream else port.get("downstream")
            if not isinstance(peer, str) or not peer.strip():
                raise RuntimeError(f"{path} link peer must be a non-empty name")
            native_vlan = _port_vlan_alias(
                port.get("native_vlan"), vlans, f"{path}.native_vlan"
            )
            if "tagged_vlans" in port:
                tagged_vlans = port["tagged_vlans"]
                if not isinstance(tagged_vlans, list) or not tagged_vlans:
                    raise RuntimeError(
                        f"{path}.tagged_vlans must be a non-empty list when set"
                    )
                for index, alias in enumerate(tagged_vlans):
                    _port_vlan_alias(
                        alias, vlans, f"{path}.tagged_vlans[{index}]"
                    )
                if len(set(tagged_vlans)) != len(tagged_vlans):
                    raise RuntimeError(
                        f"{path}.tagged_vlans contains duplicate VLAN aliases"
                    )
                if native_vlan in tagged_vlans:
                    raise RuntimeError(
                        f"{path}.tagged_vlans must not include its native VLAN"
                    )
            allowed = {
                "kind",
                "name",
                "upstream",
                "downstream",
                "native_vlan",
                "tagged_vlans",
            }

        unexpected = set(port) - allowed
        if unexpected:
            raise RuntimeError(
                f"{path} has unsupported keys: "
                f"{', '.join(sorted(map(str, unexpected)))}"
            )


def _validate_topology(
    router_name: str, switches: dict[str, dict[str, Any]], vlan_ids: dict[str, int],
) -> tuple[list[tuple[str, int]], dict[str, str], dict[str, set[str]]]:
    roots: list[tuple[str, int]] = []
    parent: dict[str, str] = {}
    children: dict[str, set[str]] = {name: set() for name in switches}

    for switch_name, switch in switches.items():
        for port_number, port in switch.get("ports", {}).items():
            if port.get("kind") != "link":
                continue
            native_vid = vlan_ids[port["native_vlan"]]
            if native_vid != 1:
                raise RuntimeError(
                    f"switches.{switch_name}.ports.{port_number} link must use "
                    "VLAN 1 (management) as its native VLAN"
                )
            if "tagged_vlans" in port and port.get("upstream") != "router":
                raise RuntimeError(
                    f"switches.{switch_name}.ports.{port_number}.tagged_vlans "
                    "is supported only on a router-facing link"
                )
            if "upstream" not in port:
                peer_name = port["downstream"]
                if peer_name not in switches:
                    raise RuntimeError(
                        f"switches.{switch_name}.ports.{port_number} references "
                        f"unknown downstream switch {peer_name!r}"
                    )
                peer_ports = [
                    (peer_number, peer)
                    for peer_number, peer in switches[peer_name].get("ports", {}).items()
                    if peer.get("kind") == "link" and peer.get("upstream") == switch_name
                ]
                if len(peer_ports) != 1:
                    raise RuntimeError(
                        f"Link {switch_name}:{port_number} must have exactly one "
                        f"reciprocal upstream link on {peer_name!r}"
                    )
                peer_number, peer = peer_ports[0]
                peer_native_vid = vlan_ids[peer["native_vlan"]]
                if native_vid != peer_native_vid:
                    raise RuntimeError(
                        f"Link {switch_name}:{port_number} to {peer_name}:{peer_number} "
                        "has mismatched native VLANs"
                    )
                if peer_name == switch_name:
                    raise RuntimeError("A switch cannot link to itself")
                if peer_name in parent and parent[peer_name] != switch_name:
                    raise RuntimeError(
                        f"Switch {peer_name!r} has ambiguous upstream paths"
                    )
                parent[peer_name] = switch_name
                children[switch_name].add(peer_name)
                continue

            peer_name = port["upstream"]
            if peer_name == "router":
                roots.append((switch_name, port_number))
                continue
            if peer_name not in switches:
                raise RuntimeError(
                    f"switches.{switch_name}.ports.{port_number} references "
                    f"unknown upstream switch {peer_name!r}"
                )
            peer_ports = [
                (peer_number, peer)
                for peer_number, peer in switches[peer_name].get("ports", {}).items()
                if peer.get("kind") == "link" and peer.get("downstream") == switch_name
            ]
            if len(peer_ports) != 1:
                raise RuntimeError(
                    f"Link {switch_name}:{port_number} must have exactly one "
                    f"reciprocal downstream link on {peer_name!r}"
                )
            peer_number, peer = peer_ports[0]
            peer_native_vid = vlan_ids[peer["native_vlan"]]
            if native_vid != peer_native_vid:
                raise RuntimeError(
                    f"Link {switch_name}:{port_number} to {peer_name}:{peer_number} "
                    "has mismatched native VLANs"
                )
            if peer_name == switch_name:
                raise RuntimeError("A switch cannot link to itself")
            if switch_name in parent and parent[switch_name] != peer_name:
                raise RuntimeError(
                    f"Switch {switch_name!r} has ambiguous upstream paths"
                )
            parent[switch_name] = peer_name
            children[peer_name].add(switch_name)

    if not roots:
        raise RuntimeError(
            f"Topology must have at least one link to router {router_name!r}"
        )
    for root_name, _root_port in roots:
        if root_name in parent and parent[root_name] != router_name:
            raise RuntimeError(
                f"Switch {root_name!r} has ambiguous upstream paths"
            )
        parent[root_name] = router_name

    for switch_name in switches:
        seen: set[str] = set()
        current = switch_name
        while current != router_name:
            if current in seen:
                raise RuntimeError(f"Topology contains a loop involving {current!r}")
            seen.add(current)
            if current not in parent:
                raise RuntimeError(
                    f"Switch {switch_name!r} has no path to router {router_name!r}"
                )
            current = parent[current]

    return roots, parent, children


def validate_desired_state(config: dict[str, Any]) -> None:
    """Validate configured VLANs, port intents, and switch topology."""
    config = _mapping(config, "configuration")
    unexpected = set(config) - _TOP_LEVEL_KEYS
    if unexpected:
        raise RuntimeError(
            f"Unsupported top-level keys: {', '.join(sorted(map(str, unexpected)))}"
        )
    if isinstance(config.get("version"), bool) or config.get("version") != 1:
        raise RuntimeError("version must be 1")
    router = _mapping(config.get("router"), "router")
    if set(router) != {"name"}:
        raise RuntimeError("router must contain only the topology root name")
    router_name = router.get("name")
    if not isinstance(router_name, str) or not router_name.strip():
        raise RuntimeError("router.name must be a non-empty string")

    _validate_vlan_sections(config)
    switches = _validate_switches(config)
    vlan_ids, _ = _vlan_indexes(config)
    _validate_topology(router_name, switches, vlan_ids)


def compile_desired_state(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compile port usage intents into normalized memberships and PVIDs."""
    validate_desired_state(config)
    switches = config["switches"]
    vlan_ids, vlan_names = _vlan_indexes(config)
    parking_vid = vlan_ids["parking"]
    router_name = config["router"]["name"]
    router_links, _parents, children = _validate_topology(
        router_name, switches, vlan_ids
    )

    state: dict[str, dict[str, Any]] = {}
    local_requirements: dict[str, set[int]] = {name: set() for name in switches}
    for switch_name, switch in switches.items():
        port_count = switch["port_count"]
        state[switch_name] = {
            "vlans": {},
            "ports": {
                number: {"pvid": parking_vid}
                for number in range(1, port_count + 1)
            },
        }
        for number, port in switch.get("ports", {}).items():
            kind = port["kind"]
            if kind == "access":
                vid = vlan_ids[port["native_vlan"]]
                _set_untagged(state[switch_name], number, vid, vlan_names)
                local_requirements[switch_name].add(vid)
            elif kind == "trunk":
                native_vid = vlan_ids[port["native_vlan"]] if "native_vlan" in port else None
                carried_vlans = list(port["vlans"])
                if "native_vlan" in port and port["native_vlan"] not in carried_vlans:
                    carried_vlans.append(port["native_vlan"])
                if native_vid is None:
                    _set_untagged(state[switch_name], number, parking_vid, vlan_names)
                    local_requirements[switch_name].add(parking_vid)
                for alias in carried_vlans:
                    vid = vlan_ids[alias]
                    _add_membership(
                        state[switch_name], vid,
                        "untagged" if vid == native_vid else "tagged",
                        number, vlan_names,
                    )
                    local_requirements[switch_name].add(vid)
                if native_vid is not None:
                    _set_pvid(state[switch_name], number, native_vid)
            elif kind == "wifi":
                native_vid = vlan_ids[port["native_vlan"]]
                _set_untagged(state[switch_name], number, native_vid, vlan_names)
                local_requirements[switch_name].add(native_vid)
                for network in config.get("wifi_networks", {}).values():
                    vid = vlan_ids[network["vlan"]]
                    if vid != native_vid:
                        _add_membership(
                            state[switch_name], vid, "tagged", number, vlan_names
                        )
                    local_requirements[switch_name].add(vid)
                _ensure_vlan_one(state[switch_name], number, native_vid, vlan_names)
            else:
                native_vid = vlan_ids[port["native_vlan"]]
                _set_untagged(state[switch_name], number, native_vid, vlan_names)
                local_requirements[switch_name].add(native_vid)
                _ensure_vlan_one(state[switch_name], number, native_vid, vlan_names)

    def subtree(switch_name: str, active: set[str]) -> set[int]:
        if switch_name in active:
            raise RuntimeError(f"Topology contains a loop involving {switch_name!r}")
        active.add(switch_name)
        required = set(local_requirements[switch_name])
        for child in sorted(children[switch_name]):
            child_required = subtree(child, active)
            required.update(child_required)
            child_port = _peer_link_port(switches[child], "upstream", switch_name)
            parent_port = _peer_link_port(switches[switch_name], "downstream", child)
            native_vid = vlan_ids[
                switches[child]["ports"][child_port]["native_vlan"]
            ]
            for vid in child_required | {1}:
                _set_link_vlan(state[child], child_port, vid, native_vid, vlan_names)
                _set_link_vlan(
                    state[switch_name], parent_port, vid, native_vid, vlan_names
                )
        active.remove(switch_name)
        required.discard(parking_vid)
        return required

    root_names = {root_name for root_name, _root_port in router_links}
    root_requirements = {
        root_name: subtree(root_name, set()) for root_name in root_names
    }
    for root_name, root_port in router_links:
        root_link = switches[root_name]["ports"][root_port]
        root_native = vlan_ids[
            root_link["native_vlan"]
        ]
        explicit_tags = {
            vlan_ids[alias] for alias in root_link.get("tagged_vlans", [])
        }
        for vid in root_requirements[root_name] | {1} | explicit_tags:
            _set_link_vlan(
                state[root_name], root_port, vid, root_native, vlan_names
            )
    return state


def _add_membership(
    state: dict[str, Any], vid: int, membership: str, port: int,
    vlan_names: dict[int, str],
) -> None:
    vlan = state["vlans"].setdefault(
        vid,
        {"name": vlan_names[vid], "tagged_ports": [], "untagged_ports": []},
    )
    ports = vlan[f"{membership}_ports"]
    if port not in ports:
        ports.append(port)
        ports.sort()


def _set_pvid(state: dict[str, Any], port: int, vid: int) -> None:
    state["ports"][port]["pvid"] = vid


def _set_untagged(
    state: dict[str, Any], port: int, vid: int, vlan_names: dict[int, str]
) -> None:
    _set_pvid(state, port, vid)
    _add_membership(state, vid, "untagged", port, vlan_names)


def _ensure_vlan_one(
    state: dict[str, Any], port: int, native_vid: int,
    vlan_names: dict[int, str],
) -> None:
    _add_membership(
        state, 1, "untagged" if native_vid == 1 else "tagged", port, vlan_names
    )


def _set_link_vlan(
    state: dict[str, Any], port: int, vid: int, native_vid: int,
    vlan_names: dict[int, str],
) -> None:
    _add_membership(
        state,
        vid,
        "untagged" if vid == native_vid else "tagged",
        port,
        vlan_names,
    )


def _peer_link_port(
    switch: dict[str, Any], direction: str, peer_name: str
) -> int:
    peer_key = "upstream" if direction == "upstream" else "downstream"
    matches = [
        number
        for number, port in switch.get("ports", {}).items()
        if port.get("kind") == "link" and port.get(peer_key) == peer_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Internal compiler error: expected one {direction} link to {peer_name!r}"
        )
    return matches[0]

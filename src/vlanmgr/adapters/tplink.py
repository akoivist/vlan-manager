"""TP-Link SDK adapter for inventory reads and guarded VLAN reconciliation."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable
from contextlib import contextmanager

from tplink_tool import make_switch


SUPPORTED_MODELS = {"TL-SG108E": 8, "TL-SG1016DE": 16}
_LOGGER = logging.getLogger(__name__)


def _password_env_name(switch_name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", switch_name).strip("_").upper()
    return f"TPLINK_PASSWORD_{suffix}"


@contextmanager
def _switch_connection(
    name: str,
    host: str,
    username: str,
    password: str,
    switch_factory: Callable[..., Any],
):
    try:
        with switch_factory(host, username=username, password=password) as switch:
            yield switch
    except Exception as error:
        message = str(error)
        normalized_message = message.casefold()
        if "login" in normalized_message and (
            "fail" in normalized_message or "check username and password" in normalized_message
        ):
            env_name = _password_env_name(name)
            raise RuntimeError(
                f"Switch {name!r}: {message} Set {env_name} to this switch's "
                f"password, or set TPLINK_PASSWORD as the shared fallback."
            ) from error
        raise


def _sdk_call(name: str, operation: str, callback: Callable[[], Any]) -> Any:
    _LOGGER.info("Switch %s: SDK operation %s starting", name, operation)
    try:
        result = callback()
    except Exception as error:
        _LOGGER.exception("Switch %s: SDK operation %s failed", name, operation)
        raise RuntimeError(
            f"Switch {name!r}: SDK operation {operation} failed: {error}"
        ) from error
    _LOGGER.info("Switch %s: SDK operation %s succeeded", name, operation)
    return result


def _model_and_revision(hardware: str) -> tuple[str, str | None]:
    for model in sorted(SUPPORTED_MODELS, key=len, reverse=True):
        if hardware == model:
            return model, None
        if hardware.startswith(f"{model} "):
            return model, hardware[len(model):].strip() or None
    raise RuntimeError(f"Unsupported or unverified switch hardware: {hardware!r}")


def _ports_from_mask(mask: int, port_count: int) -> list[int]:
    mask = int(mask)
    if mask < 0 or mask >> port_count:
        raise RuntimeError(f"VLAN membership mask {mask} exceeds {port_count} ports")
    return [port for port in range(1, port_count + 1) if mask & (1 << (port - 1))]


def _read_switch_connection(
    switch: Any, name: str, config: dict[str, Any]
) -> dict[str, Any]:
    configured_model = config.get("model")
    if configured_model is not None and configured_model not in SUPPORTED_MODELS:
        raise RuntimeError(
            f"Switch {name!r} has unsupported or unverified configured model "
            f"{configured_model!r}; verified models: {', '.join(sorted(SUPPORTED_MODELS))}"
        )

    system_info = _sdk_call(name, "get_system_info", switch.get_system_info)
    actual_model, hardware_revision = _model_and_revision(system_info.hardware)
    if configured_model is not None and actual_model != configured_model:
        raise RuntimeError(
            f"Switch {name!r} is configured as {configured_model}, "
            f"but reports {system_info.hardware!r}"
        )

    port_settings = _sdk_call(name, "get_port_settings", switch.get_port_settings)
    port_count = len(port_settings)
    if port_count == 0:
        raise RuntimeError(f"Switch {name!r} returned no port settings")
    expected_port_count = SUPPORTED_MODELS[actual_model]
    if port_count != expected_port_count:
        raise RuntimeError(
            f"Switch {name!r} ({actual_model}) returned {port_count} ports; "
            f"expected {expected_port_count}"
        )
    expected_ports = list(range(1, port_count + 1))
    if [port.port for port in port_settings] != expected_ports:
        raise RuntimeError(f"Switch {name!r} returned incomplete or unordered ports")

    pvid_values = _sdk_call(name, "get_pvids", switch.get_pvids)
    if len(pvid_values) != port_count:
        raise RuntimeError(
            f"Switch {name!r} returned {len(pvid_values)} PVIDs for "
            f"{port_count} ports"
        )

    dot1q_enabled, vlan_entries = _sdk_call(
        name, "get_dot1q_vlans", switch.get_dot1q_vlans
    )
    vlans: dict[int, dict[str, Any]] = {}
    seen_vids = set()
    for vlan in vlan_entries:
        vid = int(vlan.vid)
        if not 1 <= vid <= 4094 or vid in seen_vids:
            raise RuntimeError(f"Switch {name!r} returned invalid or duplicate VLAN {vid}")
        seen_vids.add(vid)
        tagged_ports = _ports_from_mask(vlan.tagged_members, port_count)
        untagged_ports = _ports_from_mask(vlan.untagged_members, port_count)
        if set(tagged_ports) & set(untagged_ports):
            raise RuntimeError(
                f"Switch {name!r} reports VLAN {vid} as tagged and untagged "
                "on the same port"
            )
        if actual_model == "TL-SG1016DE" and vid == 1:
            reported_untagged_ports = untagged_ports
            untagged_ports = [
                port for port in untagged_ports if int(pvid_values[port - 1]) == 1
            ]
            implicit_ports = sorted(set(reported_untagged_ports) - set(untagged_ports))
            if implicit_ports:
                _LOGGER.info(
                    "Switch %s: treating VLAN 1 untagged membership on ports %s "
                    "as implicit because their PVID is not 1",
                    name,
                    implicit_ports,
                )
        vlans[vid] = {
            "name": vlan.name or f"VLAN{vid}",
            "tagged_ports": tagged_ports,
            "untagged_ports": untagged_ports,
        }

    ports = {
        port.port: {
            "pvid": int(pvid_values[port.port - 1]),
            "enabled": port.enabled,
            "speed_configured": (
                str(port.speed_cfg) if port.speed_cfg is not None else None
            ),
            "speed_actual": str(port.speed_act) if port.speed_act is not None else None,
            "flow_control_configured": port.fc_cfg,
            "flow_control_actual": port.fc_act,
            "trunk_id": port.trunk_id,
        }
        for port in port_settings
    }

    limitations = []
    if actual_model == "TL-SG1016DE":
        limitations.append(
            "TL-SG1016DE retains VLAN 1 as an untagged member on every port; "
            "membership is effective only when the port PVID is 1"
        )

    return {
        "name": name,
        "model": actual_model,
        "hardware_revision": hardware_revision,
        "firmware": system_info.firmware,
        "limitations": limitations,
        "management_address": config["host"],
        "reported_address": system_info.ip,
        "port_count": port_count,
        "dot1q_enabled": bool(dot1q_enabled),
        "vlans": vlans,
        "ports": ports,
    }


def read_switch_state(
    name: str,
    config: dict[str, Any],
    username: str,
    password: str,
    switch_factory: Callable[..., Any] = make_switch,
) -> dict[str, Any]:
    """Read switch state into the vendor-neutral VLAN/PVID representation."""
    host = config.get("host")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Switch {name!r} has no management host")
    configured_model = config.get("model")
    if configured_model is not None and configured_model not in SUPPORTED_MODELS:
        raise RuntimeError(
            f"Switch {name!r} has unsupported or unverified configured model "
            f"{configured_model!r}; verified models: {', '.join(sorted(SUPPORTED_MODELS))}"
        )
    with _switch_connection(name, host, username, password, switch_factory) as switch:
        return _read_switch_connection(switch, name, config)


def switch_capabilities(
    name: str,
    config: dict[str, Any],
    username: str,
    password: str,
    switch_factory: Callable[..., Any] = make_switch,
) -> set[str]:
    """Return supported SDK operations without changing device state."""
    host = config.get("host")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Switch {name!r} has no management host")
    methods = {
        "add_dot1q_vlan",
        "delete_dot1q_vlan",
        "set_pvid",
        "save_config",
        "backup_config",
    }
    with _switch_connection(name, host, username, password, switch_factory) as switch:
        return {method for method in methods if callable(getattr(switch, method, None))}


def backup_switch_config(
    name: str,
    config: dict[str, Any],
    username: str,
    password: str,
    backup_path: Path,
    switch_factory: Callable[..., Any] = make_switch,
) -> bool:
    """Retain a private configuration backup, returning False if unsupported."""
    host = config.get("host")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Switch {name!r} has no management host")
    with _switch_connection(name, host, username, password, switch_factory) as switch:
        backup = getattr(switch, "backup_config", None)
        if not callable(backup):
            return False
        backup_data = _sdk_call(name, "backup_config", backup)
    if not isinstance(backup_data, bytes) or not backup_data:
        raise RuntimeError(f"Switch {name!r} returned an empty configuration backup")
    backup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as backup_file:
        backup_file.write(backup_data)
    _LOGGER.info("Switch %s: configuration backup written to %s", name, backup_path)
    return True


def apply_switch_plan(
    name: str,
    config: dict[str, Any],
    desired: dict[str, Any],
    approved_actual: dict[str, Any],
    username: str,
    password: str,
    backup_path: Path,
    backup_already_taken: bool = False,
    switch_factory: Callable[..., Any] = make_switch,
) -> dict[str, Any]:
    """Apply one approved diff, retain a backup, save, then verify live state."""
    host = config.get("host")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Switch {name!r} has no management host")

    with _switch_connection(name, host, username, password, switch_factory) as switch:
        actual = _read_switch_connection(switch, name, config)
        from vlanmgr.reconcile import semantic_diff

        if semantic_diff(
            {name: desired}, {name: actual}, {name: switch_capabilities_from(switch)}
        )[name] != semantic_diff(
            {name: desired}, {name: approved_actual}, {name: switch_capabilities_from(switch)}
        )[name]:
            raise RuntimeError(
                f"Switch {name!r} changed after confirmation; refusing stale plan"
            )
        plan = semantic_diff(
            {name: desired}, {name: actual}, {name: switch_capabilities_from(switch)}
        )[name]
        if plan["unsupported_behavior"]:
            raise RuntimeError(
                f"Switch {name!r} has unsupported behavior: "
                f"{'; '.join(plan['unsupported_behavior'])}"
            )
        has_changes = any((
            plan["vlans"]["create"], plan["vlans"]["update"],
            plan["vlans"]["delete"], plan["pvids"],
        ))
        if not has_changes:
            return actual

        if not backup_already_taken:
            backup_switch_config(
                name, config, username, password, backup_path, switch_factory
            )

        def removes_untagged_membership(change: dict[str, Any]) -> bool:
            return any(
                membership["vid"] == change["vid"]
                and membership["from"] == "untagged"
                and membership["to"] != "untagged"
                for membership in plan["memberships"]
            )

        membership_removal_updates = [
            change for change in plan["vlans"]["update"]
            if removes_untagged_membership(change)
        ]
        vlan_one_removal_updates = [
            change for change in membership_removal_updates
            if change["vid"] == 1
        ]
        membership_removal_updates = [
            change for change in membership_removal_updates
            if change["vid"] != 1
        ]
        other_updates = [
            change for change in plan["vlans"]["update"]
            if not removes_untagged_membership(change)
        ]

        for change in membership_removal_updates:
            vlan = change["to"]
            _sdk_call(
                name,
                f"add_dot1q_vlan vid={change['vid']} (remove old untagged membership)",
                lambda change=change, vlan=vlan: switch.add_dot1q_vlan(
                    change["vid"], vlan["name"],
                    tagged_ports=vlan["tagged_ports"],
                    untagged_ports=vlan["untagged_ports"],
                ),
            )
        for vlan in plan["vlans"]["create"]:
            _sdk_call(
                name,
                f"add_dot1q_vlan vid={vlan['vid']}",
                lambda vlan=vlan: switch.add_dot1q_vlan(
                    vlan["vid"], vlan["name"],
                    tagged_ports=vlan["tagged_ports"],
                    untagged_ports=vlan["untagged_ports"],
                ),
            )
        for change in other_updates:
            vlan = change["to"]
            _sdk_call(
                name,
                f"add_dot1q_vlan vid={change['vid']} (update)",
                lambda change=change, vlan=vlan: switch.add_dot1q_vlan(
                    change["vid"], vlan["name"],
                    tagged_ports=vlan["tagged_ports"],
                    untagged_ports=vlan["untagged_ports"],
                ),
            )
        pvid_groups: dict[int, list[int]] = {}
        for change in plan["pvids"]:
            pvid_groups.setdefault(change["to"], []).append(change["port"])
        for vid, ports in sorted(pvid_groups.items()):
            _sdk_call(
                name,
                f"set_pvid ports={ports} pvid={vid}",
                lambda ports=ports, vid=vid: switch.set_pvid(ports, vid),
            )
        vlan_one = desired["vlans"].get(1)
        if vlan_one is not None and (pvid_groups or vlan_one_removal_updates):
            _sdk_call(
                name,
                "add_dot1q_vlan vid=1 (reconcile after PVID update)",
                lambda vlan=vlan_one: switch.add_dot1q_vlan(
                    1, vlan["name"],
                    tagged_ports=vlan["tagged_ports"],
                    untagged_ports=vlan["untagged_ports"],
                ),
            )
        for vlan in plan["vlans"]["delete"]:
            _sdk_call(
                name,
                f"delete_dot1q_vlan vid={vlan['vid']}",
                lambda vlan=vlan: switch.delete_dot1q_vlan(vlan["vid"]),
            )
        _sdk_call(name, "save_config", switch.save_config)

    _LOGGER.info("Switch %s: reading state back for post-apply verification", name)
    verified = read_switch_state(
        name, config, username, password, switch_factory=switch_factory
    )
    remaining = semantic_diff(
        {name: desired}, {name: verified}, {name: switch_capabilities(
            name, config, username, password, switch_factory
        )}
    )[name]
    if any((
        remaining["vlans"]["create"], remaining["vlans"]["update"],
        remaining["vlans"]["delete"], remaining["memberships"], remaining["pvids"],
    )):
        _LOGGER.error(
            "Switch %s: post-apply verification failed; remaining diff: %s",
            name,
            remaining,
        )
        raise RuntimeError(
            f"Switch {name!r} failed post-apply verification; remaining diff: "
            f"{remaining}"
        )
    _LOGGER.info("Switch %s: post-apply verification succeeded", name)
    return verified


def switch_capabilities_from(switch: Any) -> set[str]:
    names = (
        "add_dot1q_vlan",
        "delete_dot1q_vlan",
        "set_pvid",
        "save_config",
    )
    return {name for name in names if callable(getattr(switch, name, None))}


def collect_switch_baseline(
    name: str,
    config: dict[str, Any],
    username: str,
    password: str,
    switch_factory: Callable[..., Any] = make_switch,
) -> dict[str, Any]:
    """Compatibility name for the normalized read-only switch-state reader."""
    return read_switch_state(name, config, username, password, switch_factory)
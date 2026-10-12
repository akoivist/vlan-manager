"""Vendor-neutral planning and execution of VLAN/PVID reconciliation."""

from __future__ import annotations

from typing import Any


def semantic_diff(
    desired: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
    capabilities: dict[str, set[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compare compiled desired state to normalized live state."""
    result: dict[str, dict[str, Any]] = {}
    capabilities = capabilities or {}
    for switch_name, desired_switch in desired.items():
        actual_switch = actual[switch_name]
        desired_vlans = desired_switch["vlans"]
        actual_vlans = actual_switch["vlans"]
        create = []
        update = []
        delete = []
        membership_changes = []

        for vid, desired_vlan in sorted(desired_vlans.items()):
            actual_vlan = actual_vlans.get(vid)
            if actual_vlan is None:
                create.append({"vid": vid, **desired_vlan})
                for port in desired_vlan["tagged_ports"]:
                    membership_changes.append({
                        "port": port, "vid": vid, "from": "none", "to": "tagged"
                    })
                for port in desired_vlan["untagged_ports"]:
                    membership_changes.append({
                        "port": port, "vid": vid, "from": "none", "to": "untagged"
                    })
                continue

            old_tagged = set(actual_vlan["tagged_ports"])
            old_untagged = set(actual_vlan["untagged_ports"])
            new_tagged = set(desired_vlan["tagged_ports"])
            new_untagged = set(desired_vlan["untagged_ports"])
            if (
                actual_vlan["name"] != desired_vlan["name"]
                or old_tagged != new_tagged
                or old_untagged != new_untagged
            ):
                update.append({
                    "vid": vid,
                    "from": actual_vlan,
                    "to": desired_vlan,
                })
            for port in sorted(old_tagged | old_untagged | new_tagged | new_untagged):
                old_kind = (
                    "tagged" if port in old_tagged
                    else "untagged" if port in old_untagged else "none"
                )
                new_kind = (
                    "tagged" if port in new_tagged
                    else "untagged" if port in new_untagged else "none"
                )
                if old_kind != new_kind:
                    membership_changes.append({
                        "port": port,
                        "vid": vid,
                        "from": old_kind,
                        "to": new_kind,
                    })

        for vid, actual_vlan in sorted(actual_vlans.items()):
            if vid not in desired_vlans and vid != 1:
                delete.append({"vid": vid, **actual_vlan})
                for port in actual_vlan["tagged_ports"]:
                    membership_changes.append({
                        "port": port, "vid": vid, "from": "tagged", "to": "none"
                    })
                for port in actual_vlan["untagged_ports"]:
                    membership_changes.append({
                        "port": port, "vid": vid, "from": "untagged", "to": "none"
                    })

        desired_pvids = {
            port: state["pvid"]
            for port, state in desired_switch["ports"].items()
        }
        actual_pvids = {
            port: state["pvid"]
            for port, state in actual_switch["ports"].items()
        }
        pvid_changes = [
            {"port": port, "from": actual_pvids[port], "to": vid}
            for port, vid in sorted(desired_pvids.items())
            if actual_pvids.get(port) != vid
        ]

        missing_operations = sorted({
            method
            for method, needed in (
                ("add_dot1q_vlan", bool(create or update)),
                ("delete_dot1q_vlan", bool(delete)),
                ("set_pvid", bool(pvid_changes)),
                (
                    "save_config",
                    bool(create or update or delete or pvid_changes),
                ),
            )
            if needed and method not in capabilities.get(switch_name, set())
        })
        unsupported = []
        unsupported.extend(
            f"Required SDK operation {method} is unavailable"
            for method in missing_operations
        )
        warnings = [
            *actual_switch.get("warnings", []),
            *actual_switch.get("limitations", []),
        ]
        has_pending_changes = any((
            create, update, delete, membership_changes, pvid_changes,
        ))
        if has_pending_changes and "backup_config" not in capabilities.get(switch_name, set()):
            warnings.append("SDK does not support configuration backup for this switch")
        if has_pending_changes and not actual_switch.get("dot1q_enabled", True):
            unsupported.append(
                "802.1Q VLAN mode is disabled; the reconciler will not change it"
            )

        result[switch_name] = {
            "vlans": {"create": create, "update": update, "delete": delete},
            "memberships": membership_changes,
            "pvids": pvid_changes,
            "unsupported_behavior": unsupported,
            "warnings": warnings,
        }
    return result


def has_changes(plan: dict[str, dict[str, Any]]) -> bool:
    return any(
        item["vlans"]["create"]
        or item["vlans"]["update"]
        or item["vlans"]["delete"]
        or item["memberships"]
        or item["pvids"]
        for item in plan.values()
    )

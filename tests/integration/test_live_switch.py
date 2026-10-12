"""Opt-in destructive integration tests using the vlanmgr CLI against a switch.

Run with VLANMGR_LIVE_TESTS=1 after setting the target and switch credentials.
The suite changes ports 2-4 and propagates test VLANs over upstream port 1,
which must retain VLAN 1 untagged. Every generated test intent is compiled by
the CLI and checked against its target before the CLI apply command can write.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from vlanmgr import cli
from vlanmgr.adapters.tplink import read_switch_state


_LOGGER = logging.getLogger(__name__)
_TEST_PORTS = (2, 3, 4)
_PARKING_VLAN_NAME = "vlmgr-parking-"


def _device_call(
    name: str, password: str, operation: str, callback: Callable[[], Any]
) -> Any:
    try:
        return callback()
    except Exception as exc:
        message = str(exc)
        if password:
            message = message.replace(password, "<redacted>")
        pytest.fail(
            f"Live switch {name!r}: {operation} failed: {message}",
            pytrace=False,
        )


def test_live_device_errors_redact_passwords() -> None:
    password = "test-secret"

    def raise_sdk_error() -> None:
        raise RuntimeError(f"SDK error included {password}")

    with pytest.raises(BaseException) as result:
        _device_call(
            "test-switch",
            password,
            "test operation",
            raise_sdk_error,
        )

    assert password not in str(result.value)
    assert "<redacted>" in str(result.value)


def test_live_test_intent_compiles_to_exact_vendor_neutral_state(
    tmp_path: Path,
) -> None:
    desired = {
        "vlans": {
            1: {
                "name": "management",
                "tagged_ports": [],
                "untagged_ports": [1, 2, 5, 6, 7, 8],
            },
            20: {
                "name": "test",
                "tagged_ports": [1, 4],
                "untagged_ports": [3],
            },
            30: {
                "name": "routed-only",
                "tagged_ports": [1],
                "untagged_ports": [],
            },
            4094: {
                "name": "parking",
                "tagged_ports": [],
                "untagged_ports": [4],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 1},
            3: {"pvid": 20},
            4: {"pvid": 4094},
            **{port: {"pvid": 1} for port in range(5, 9)},
        },
    }
    target = LiveSwitch(
        name="test-switch",
        config={"host": "192.0.2.1"},
        username="admin",
        password="unused",
        config_path=tmp_path / "config.yaml",
        backup_dir=tmp_path / "backups",
        run_dir=tmp_path,
        original={"model": "TL-SG108E", "port_count": 8},
        test_vids=(4000, 4001),
        parking_vid=4094,
    )

    config = _config_from_desired(target, desired)
    compiled = yaml.safe_load(_run_cli(target, "compile", config))

    assert config["switches"]["test-switch"]["ports"][1]["tagged_vlans"] == [
        "vlan_20",
        "vlan_30",
    ]
    assert compiled == {"test-switch": desired}
    assert 4 not in compiled["test-switch"]["vlans"][4094]["tagged_ports"]
    assert 1 not in compiled["test-switch"]["vlans"][4094]["tagged_ports"]


def test_parking_vlan_must_not_be_selected_from_active_vlan_pvid() -> None:
    desired = {
        "vlans": {
            1: {
                "name": "management",
                "tagged_ports": [],
                "untagged_ports": [1],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 1},
            **{port: {"pvid": 998} for port in range(3, 9)},
        },
    }

    assert _parking_vid_for_state(desired) == 998


def test_existing_generated_parking_vlan_is_recognized_and_not_propagated(
    tmp_path: Path,
) -> None:
    desired = {
        "vlans": {
            1: {
                "name": "lan1921680",
                "tagged_ports": [5, 6, 7, 8],
                "untagged_ports": [1, 2],
            },
            2: {
                "name": "lan1921681",
                "tagged_ports": [1],
                "untagged_ports": [5, 6, 7, 8],
            },
            4000: {
                "name": "vlmgr-test-a",
                "tagged_ports": [1, 3, 4],
                "untagged_ports": [],
            },
            4001: {
                "name": "vlmgr-test-b",
                "tagged_ports": [1, 4],
                "untagged_ports": [3],
            },
            4094: {
                "name": _PARKING_VLAN_NAME,
                "tagged_ports": [],
                "untagged_ports": [4],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 1},
            3: {"pvid": 4001},
            4: {"pvid": 4094},
            **{port: {"pvid": 2} for port in range(5, 9)},
        },
    }

    parking_vid = _parking_vid_for_state(desired)
    target = LiveSwitch(
        name="test-switch",
        config={"host": "192.0.2.1"},
        username="admin",
        password="",
        config_path=tmp_path / "config.yaml",
        backup_dir=tmp_path / "backups",
        run_dir=tmp_path,
        original={"model": "TL-SG108E", "port_count": 8},
        test_vids=(4002, 4003),
        parking_vid=parking_vid,
    )
    config = _config_from_desired(target, desired)
    compiled = yaml.safe_load(_run_cli(target, "compile", config))["test-switch"]

    assert parking_vid == 4094
    assert config["vlans"]["parking"]["id"] == 4094
    assert compiled["vlans"][4094]["untagged_ports"] == [4]
    assert compiled["vlans"][4094]["tagged_ports"] == []


def test_live_state_ignores_stale_vlan_one_membership_when_pvid_is_not_one() -> None:
    live = {
        "vlans": {
            1: {
                "name": "management",
                "tagged_ports": [],
                "untagged_ports": [1, 2, 3],
            },
            4001: {
                "name": "test",
                "tagged_ports": [],
                "untagged_ports": [2, 3],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 4001},
            3: {"pvid": 4001},
        },
    }

    effective = _desired_from_live(live)

    assert effective["vlans"][1]["untagged_ports"] == [1]
    assert effective["vlans"][4001]["untagged_ports"] == [2, 3]
    assert live["vlans"][1]["untagged_ports"] == [1, 2, 3]


def test_live_restore_opt_out_requires_explicit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLANMGR_LIVE_SKIP_RESTORE", raising=False)
    assert not _live_restore_disabled()

    monkeypatch.setenv("VLANMGR_LIVE_SKIP_RESTORE", "true")
    assert not _live_restore_disabled()

    monkeypatch.setenv("VLANMGR_LIVE_SKIP_RESTORE", "1")
    assert _live_restore_disabled()


def test_live_state_summary_logs_port_memberships(caplog: pytest.LogCaptureFixture) -> None:
    state = {
        "vlans": {
            1: {"tagged_ports": [3], "untagged_ports": [1]},
            20: {"tagged_ports": [], "untagged_ports": [3]},
        },
        "ports": {
            1: {"pvid": 1},
            3: {"pvid": 20},
        },
    }

    with caplog.at_level(logging.ERROR, logger=__name__):
        _log_state_summary("switch", state, label="observed")

    assert "'pvid': 1" in caplog.text
    assert "'tagged': [1]" in caplog.text
    assert "'untagged': [20]" in caplog.text
    assert "tagged_ports" in caplog.text


def test_upstream_compilation_may_only_add_nonparking_tagged_vlans() -> None:
    expected = {
        "vlans": {
            1: {"name": "management", "tagged_ports": [], "untagged_ports": [1]},
            2: {"name": "home", "tagged_ports": [], "untagged_ports": [2]},
            998: {
                "name": "parking",
                "tagged_ports": [],
                "untagged_ports": [3],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 2},
            3: {"pvid": 998},
        },
    }
    compiled = copy.deepcopy(expected)
    compiled["vlans"][2]["tagged_ports"] = [1]

    normalized = _with_allowed_upstream_tagged_vlans(expected, compiled, 998)

    assert normalized == compiled


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["vlans"][998]["tagged_ports"].append(1),
        lambda state: state["vlans"][2]["tagged_ports"].append(2),
        lambda state: state["vlans"][1]["untagged_ports"].remove(1),
        lambda state: state["ports"][1].update(pvid=2),
    ],
)
def test_upstream_compilation_rejects_other_state_changes(mutate) -> None:
    expected = {
        "vlans": {
            1: {"name": "management", "tagged_ports": [], "untagged_ports": [1]},
            2: {"name": "home", "tagged_ports": [], "untagged_ports": [2]},
            998: {
                "name": "parking",
                "tagged_ports": [],
                "untagged_ports": [3],
            },
        },
        "ports": {
            1: {"pvid": 1},
            2: {"pvid": 2},
            3: {"pvid": 998},
        },
    }
    compiled = copy.deepcopy(expected)
    mutate(compiled)

    assert _with_allowed_upstream_tagged_vlans(expected, compiled, 998) is None


def test_test_vlan_ids_do_not_overlap_live_pvids() -> None:
    live = {
        "vlans": {vid: {} for vid in range(4000, 4001)},
        "ports": {3: {"pvid": 4001}},
    }

    assert _available_test_vids(live) == (4002, 4003)


def test_test_vlan_names_do_not_collide_with_retained_live_vlan_names() -> None:
    live = {
        "vlans": {
            1: {"name": "management"},
            3999: {"name": "vlmgr-test-a"},
            4000: {"name": "vlmgr-test-b"},
        }
    }

    names = _available_test_vlan_names(live, (4001, 4002))

    assert names == ("vlmgr-a-4001", "vlmgr-b-4002")
    assert len({name.casefold() for name in names}) == len(names)


@dataclass
class LiveSwitch:
    name: str
    config: dict[str, Any]
    username: str
    password: str = field(repr=False)
    config_path: Path
    backup_dir: Path
    run_dir: Path
    original: dict[str, Any]
    test_vids: tuple[int, int]
    parking_vid: int

    def __repr__(self) -> str:
        return (
            f"LiveSwitch(name={self.name!r}, "
            f"host={self.config.get('host')!r})"
        )


def _desired_from_live(live: dict[str, Any]) -> dict[str, Any]:
    desired = {
        "vlans": copy.deepcopy(live["vlans"]),
        "ports": {
            port: {"pvid": state["pvid"]}
            for port, state in live["ports"].items()
        },
    }
    vlan_one = desired["vlans"].get(1)
    if vlan_one is not None:
        vlan_one["untagged_ports"] = [
            port
            for port in vlan_one["untagged_ports"]
            if desired["ports"][port]["pvid"] == 1
        ]
    return desired


def _without_empty_parking_vlan(
    desired: dict[str, Any], parking_vid: int
) -> dict[str, Any]:
    normalized = copy.deepcopy(desired)
    parking = normalized["vlans"].get(parking_vid)
    if parking is not None and not (
        parking["tagged_ports"] or parking["untagged_ports"]
    ):
        del normalized["vlans"][parking_vid]
    return normalized


def _live_restore_disabled() -> bool:
    return os.environ.get("VLANMGR_LIVE_SKIP_RESTORE") == "1"


def _log_state_summary(
    name: str, state: dict[str, Any], *, label: str
) -> None:
    _LOGGER.error(
        "Live switch %s %s: ports=%s VLANs=%s",
        name,
        label,
        {
            port: {
                "pvid": port_state["pvid"],
                "tagged": sorted(
                    vid for vid, vlan in state["vlans"].items()
                    if port in vlan["tagged_ports"]
                ),
                "untagged": sorted(
                    vid for vid, vlan in state["vlans"].items()
                    if port in vlan["untagged_ports"]
                ),
            }
            for port, port_state in sorted(state["ports"].items())
        },
        {
            vid: {
                "tagged_ports": vlan["tagged_ports"],
                "untagged_ports": vlan["untagged_ports"],
            }
            for vid, vlan in sorted(state["vlans"].items())
        },
    )


def _parking_vid_for_state(desired: dict[str, Any]) -> int:
    named_parking = [
        vid
        for vid, vlan in desired["vlans"].items()
        if vlan["name"].casefold().startswith(_PARKING_VLAN_NAME.casefold())
    ]
    if len(named_parking) > 1:
        raise RuntimeError(
            "The live switch state contains multiple generated parking VLANs: "
            f"{sorted(named_parking)}"
        )
    if named_parking:
        return named_parking[0]

    memberships = {
        port: {
            "tagged": [
                vid for vid, vlan in desired["vlans"].items()
                if port in vlan["tagged_ports"]
            ],
            "untagged": [
                vid for vid, vlan in desired["vlans"].items()
                if port in vlan["untagged_ports"]
            ],
        }
        for port in desired["ports"]
    }
    member_vids = {
        vid for vid, vlan in desired["vlans"].items()
        if vlan["tagged_ports"] or vlan["untagged_ports"]
    }
    candidates = {
        desired["ports"][port]["pvid"]
        for port, membership in memberships.items()
        if not membership["untagged"]
        and desired["ports"][port]["pvid"] not in membership["tagged"]
        and (
            desired["ports"][port]["pvid"] not in member_vids
            or (
                desired["ports"][port]["pvid"] != 1
                and any(
                    desired["ports"][port]["pvid"] == vid
                    and any(
                        desired["ports"][member]["pvid"] == vid
                        for member in vlan["untagged_ports"]
                    )
                    for vid, vlan in desired["vlans"].items()
                )
            )
        )
    }
    if len(candidates) > 1:
        raise RuntimeError(
            "The CLI configuration cannot represent multiple PVID-only VLANs: "
            f"{sorted(candidates)}"
        )
    if candidates:
        return next(iter(candidates))
    pvids = {state["pvid"] for state in desired["ports"].values()}
    return next(
        (
            vid for vid in range(4094, 0, -1)
            if vid not in desired["vlans"] and vid not in pvids
        ),
        0,
    )


def _available_test_vids(live: dict[str, Any]) -> tuple[int, int]:
    occupied = set(live["vlans"])
    occupied.update(port["pvid"] for port in live["ports"].values())
    available = [
        vid for vid in range(4000, 4095) if vid not in occupied
    ]
    if len(available) < 2:
        raise RuntimeError(
            "No two unused test VLAN IDs are available in range 4000-4094 "
            "after excluding existing VLANs and PVIDs"
        )
    return available[0], available[1]


def _available_test_vlan_names(
    live: dict[str, Any], test_vids: tuple[int, int]
) -> tuple[str, str]:
    used_names = {
        vlan["name"].casefold() for vlan in live["vlans"].values()
    }
    names: list[str] = []
    for label, vid in zip(("a", "b"), test_vids):
        base = f"vlmgr-{label}-{vid}"
        for suffix_number in range(1000):
            if suffix_number:
                suffix = f"-{suffix_number}"
                candidate = f"{base[:14 - len(suffix)]}{suffix}"
            else:
                candidate = base
            if candidate.casefold() not in used_names:
                break
        else:
            raise RuntimeError(
                f"Could not create a unique temporary VLAN name for VLAN {vid}"
            )
        used_names.add(candidate.casefold())
        names.append(candidate)
    return names[0], names[1]


def _config_from_desired(
    target: LiveSwitch, desired: dict[str, Any]
) -> dict[str, Any]:
    vlans = desired["vlans"]
    ports = desired["ports"]
    if 1 not in vlans or 1 not in vlans[1]["untagged_ports"]:
        raise RuntimeError("The CLI test intent must retain port 1 untagged in VLAN 1")
    if ports[1]["pvid"] != 1:
        raise RuntimeError("The CLI test intent must retain port 1 PVID 1")

    parking_vid = target.parking_vid
    if not parking_vid:
        raise RuntimeError("No VLAN ID is available for the CLI parking alias")
    aliases = {vid: f"vlan_{vid}" for vid in vlans}
    aliases[parking_vid] = "parking"
    vlan_config = {
        aliases[vid]: {"id": vid, "name": vlan["name"]}
        for vid, vlan in vlans.items()
        if vid != parking_vid
    }
    vlan_config.setdefault(
        "parking",
        {
            "id": parking_vid,
            "name": vlans.get(parking_vid, {}).get(
                "name", _PARKING_VLAN_NAME
            ),
        },
    )
    name_to_vids: dict[str, list[int]] = {}
    for item in vlan_config.values():
        name_to_vids.setdefault(item["name"].casefold(), []).append(item["id"])
    duplicate_names = {
        name: vids for name, vids in name_to_vids.items() if len(vids) > 1
    }
    if duplicate_names:
        raise RuntimeError(
            "The CLI configuration cannot represent VLANs with duplicate names: "
            + "; ".join(
                f"{name!r} on VLAN IDs {sorted(vids)}"
                for name, vids in sorted(duplicate_names.items())
            )
        )

    upstream_tagged_vlans = [
        aliases[vid]
        for vid, vlan in sorted(vlans.items())
        if vid != 1
        and vid != parking_vid
        and 1 in vlan["tagged_ports"]
    ]
    port_config: dict[int, dict[str, Any]] = {
        1: {
            "kind": "link",
            "upstream": "router",
            "native_vlan": aliases[1],
        }
    }
    if upstream_tagged_vlans:
        port_config[1]["tagged_vlans"] = upstream_tagged_vlans
    for port, port_state in ports.items():
        if port == 1:
            continue
        tagged = sorted(
            vid for vid, vlan in vlans.items() if port in vlan["tagged_ports"]
        )
        untagged = sorted(
            vid for vid, vlan in vlans.items() if port in vlan["untagged_ports"]
        )
        if len(untagged) > 1:
            raise RuntimeError(
                f"The CLI configuration cannot represent port {port} as untagged "
                f"in multiple VLANs: {untagged}"
            )
        pvid = port_state["pvid"]
        if untagged:
            native_vid = untagged[0]
            if pvid != native_vid:
                raise RuntimeError(
                    f"The CLI configuration cannot represent port {port}: "
                    f"PVID {pvid} differs from untagged VLAN {native_vid}"
                )
            if native_vid == parking_vid:
                if not tagged:
                    raise RuntimeError(
                        f"The CLI configuration cannot represent port {port} "
                        "with only an untagged parking VLAN"
                    )
                port_config[port] = {
                    "kind": "trunk",
                    "vlans": [aliases[vid] for vid in tagged],
                }
            elif tagged:
                carried = sorted(set(tagged + [native_vid]))
                port_config[port] = {
                    "kind": "trunk",
                    "vlans": [aliases[vid] for vid in carried],
                    "native_vlan": aliases[native_vid],
                }
            else:
                port_config[port] = {
                    "kind": "access",
                    "native_vlan": aliases[native_vid],
                }
        elif tagged:
            if pvid != parking_vid:
                raise RuntimeError(
                    f"The CLI configuration cannot represent port {port}: "
                    f"tagged-only memberships require parking PVID {parking_vid}, "
                    f"not {pvid}"
                )
            port_config[port] = {
                "kind": "trunk",
                "vlans": [aliases[vid] for vid in tagged],
            }
        elif pvid != parking_vid:
            raise RuntimeError(
                f"The CLI configuration cannot represent unused port {port} "
                f"with PVID {pvid}; expected parking PVID {parking_vid}"
            )

    config = {
        "version": 1,
        "router": {"name": "live-test-router"},
        "vlans": vlan_config,
        "switches": {
            target.name: {
                "host": target.config["host"],
                "model": target.original["model"],
                "port_count": target.original["port_count"],
                "ports": port_config,
            }
        },
    }
    return config


def _run_cli(target: LiveSwitch, command: str, config: dict[str, Any]) -> str:
    target.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    source_path = str(project_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (source_path, existing_pythonpath) if path
    )
    arguments = [
        sys.executable,
        "-m",
        "vlanmgr.cli",
        command,
        "--config",
        str(target.config_path),
    ]
    if command == "apply":
        arguments.extend(("--switch", target.name))
    if command == "apply":
        arguments.extend(("--backup-dir", str(target.backup_dir)))
    result = subprocess.run(
        arguments,
        input="APPLY\n" if command == "apply" else None,
        capture_output=True,
        check=False,
        cwd=project_root,
        env=environment,
        text=True,
    )
    output = result.stdout + result.stderr
    if target.password:
        output = output.replace(target.password, "<redacted>")
    if result.returncode:
        raise RuntimeError(
            f"vlanmgr {command} failed with exit code {result.returncode}:\n{output}",
        )
    return result.stdout


def _set_port_memberships(
    state: dict[str, Any],
    port: int,
    *,
    tagged: tuple[int, ...] = (),
    untagged: int | None = None,
) -> None:
    for vlan in state["vlans"].values():
        vlan["tagged_ports"] = [
            member for member in vlan["tagged_ports"] if member != port
        ]
        vlan["untagged_ports"] = [
            member for member in vlan["untagged_ports"] if member != port
        ]
    for vid in tagged:
        state["vlans"][vid]["tagged_ports"].append(port)
        state["vlans"][vid]["tagged_ports"].sort()
    if untagged is not None:
        state["vlans"][untagged]["untagged_ports"].append(port)
        state["vlans"][untagged]["untagged_ports"].sort()
        state["ports"][port]["pvid"] = untagged


def _with_allowed_upstream_tagged_vlans(
    expected: dict[str, Any],
    compiled: dict[str, Any],
    parking_vid: int,
) -> dict[str, Any] | None:
    if set(expected) != set(compiled):
        return None
    normalized = copy.deepcopy(expected)
    expected_vlans = normalized["vlans"]
    compiled_vlans = compiled.get("vlans")
    if not isinstance(compiled_vlans, dict) or set(expected_vlans) != set(
        compiled_vlans
    ):
        return None
    if normalized["ports"] != compiled.get("ports"):
        return None

    added_upstream_tags: dict[int, list[int]] = {}
    for vid, expected_vlan in expected_vlans.items():
        compiled_vlan = compiled_vlans[vid]
        if expected_vlan["name"] != compiled_vlan.get("name"):
            return None
        if expected_vlan["untagged_ports"] != compiled_vlan.get("untagged_ports"):
            return None

        expected_tagged = set(expected_vlan["tagged_ports"])
        compiled_tagged = set(compiled_vlan.get("tagged_ports", []))
        added = compiled_tagged - expected_tagged
        if not expected_tagged <= compiled_tagged:
            return None
        if added:
            if vid == parking_vid or added != {1}:
                return None
            added_upstream_tags[vid] = sorted(added)
        expected_vlan["tagged_ports"] = sorted(compiled_tagged)

    if normalized != compiled:
        return None
    if added_upstream_tags:
        _LOGGER.warning(
            "CLI topology compilation adds non-parking VLANs as tagged "
            "members on upstream port 1: %s",
            sorted(added_upstream_tags),
        )
    return normalized


def _apply_desired(target: LiveSwitch, desired: dict[str, Any]) -> dict[str, Any]:
    config = _config_from_desired(target, desired)
    compiled = yaml.safe_load(_run_cli(target, "compile", config))
    normalized_desired = _without_empty_parking_vlan(desired, target.parking_vid)
    compiled_switch = compiled.get(target.name) if isinstance(compiled, dict) else None
    if not isinstance(compiled_switch, dict):
        _LOGGER.error(
            "CLI compilation did not return a state for switch %s: %r",
            target.name, compiled,
        )
        _log_state_summary(
            target.name, normalized_desired, label="requested"
        )
        raise RuntimeError(
            "Generated CLI YAML did not compile to a switch state; "
            "refusing device writes"
        )
    applied_desired = _with_allowed_upstream_tagged_vlans(
        normalized_desired, compiled_switch, target.parking_vid
    )
    if applied_desired is None:
        _LOGGER.error(
            "Generated live-test CLI intent differs from the requested target "
            "for switch %s",
            target.name,
        )
        _log_state_summary(target.name, normalized_desired, label="requested")
        _log_state_summary(target.name, compiled_switch, label="compiled")
        raise RuntimeError(
            "Generated CLI YAML does not compile to the live-test target except "
            "for permitted upstream tagged VLAN propagation; refusing device writes"
        )
    apply_output = _run_cli(target, "apply", config)
    if (
        "Apply writes completed" in apply_output
        and not list(target.backup_dir.glob("*.bin"))
    ):
        raise RuntimeError(
            "vlanmgr apply completed without retaining a pre-write backup",
        )
    return applied_desired


def _assert_upstream_unchanged(
    state: dict[str, Any],
    original: dict[str, Any],
    *,
    parking_vid: int | None = None,
    allow_added_tagged: bool = False,
) -> None:
    assert state["ports"][1]["pvid"] == original["ports"][1]["pvid"] == 1
    all_vids = set(original["vlans"]) | set(state["vlans"])
    for vid in all_vids:
        original_vlan = original["vlans"].get(
            vid, {"tagged_ports": [], "untagged_ports": []}
        )
        state_vlan = state["vlans"].get(
            vid, {"tagged_ports": [], "untagged_ports": []}
        )
        assert (1 in state_vlan["untagged_ports"]) == (
            1 in original_vlan["untagged_ports"]
        )
        was_tagged = 1 in original_vlan["tagged_ports"]
        is_tagged = 1 in state_vlan["tagged_ports"]
        assert is_tagged or not was_tagged
        if not allow_added_tagged:
            assert is_tagged == was_tagged
        if is_tagged and parking_vid is not None:
            assert vid != parking_vid
    assert 1 in state["vlans"][1]["untagged_ports"]


def _restore_test_scope(target: LiveSwitch) -> dict[str, Any]:
    return _apply_desired(target, _desired_from_live(target.original))


@pytest.fixture
def live_switch() -> Any:
    __tracebackhide__ = True
    name = os.environ.get("VLANMGR_LIVE_SWITCH")
    host = os.environ.get("VLANMGR_LIVE_HOST")
    if not host:
        pytest.fail("Set VLANMGR_LIVE_HOST to the test switch management address")

    config: dict[str, Any] = {"host": host}
    model = os.environ.get("VLANMGR_LIVE_MODEL")
    if model:
        config["model"] = model
    username = os.environ.get("TPLINK_USERNAME", "admin")
    passwords = cli._switch_passwords({"switches": {name: config}})
    password = passwords[name]

    original = _device_call(
        name,
        password,
        "initial state read",
        lambda: read_switch_state(name, config, username, password),
    )
    if original["model"] != "TL-SG108E":
        pytest.skip(
            "Live create/update/delete integration coverage currently requires "
            "TL-SG108E"
        )
    if original["port_count"] < max(_TEST_PORTS):
        pytest.skip(
            f"Live tests require ports {_TEST_PORTS}; device has "
            f"{original['port_count']} ports"
        )
    if original["ports"][1]["pvid"] != 1:
        pytest.fail("Port 1 must have PVID 1 and remain the upstream link")
    vlan_one = original["vlans"].get(1)
    if vlan_one is None or 1 not in vlan_one["untagged_ports"]:
        pytest.fail("Port 1 must be untagged in VLAN 1 before live tests")

    try:
        test_vids = _available_test_vids(original)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower()
    backup_root = Path(
        os.environ.get("VLANMGR_LIVE_BACKUP_DIR", "backups/live-tests")
    )
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{safe_name}-{stamp}-", dir=backup_root))
    backup_dir = run_dir / "backups"
    backup_dir.mkdir(mode=0o700)

    target = LiveSwitch(
        name=name,
        config=config,
        username=username,
        password=password,
        config_path=run_dir / "config.yaml",
        backup_dir=backup_dir,
        run_dir=run_dir,
        original=original,
        test_vids=test_vids,
        parking_vid=_parking_vid_for_state(_desired_from_live(original)),
    )
    original_desired = _desired_from_live(original)
    skip_restore = _live_restore_disabled()
    if skip_restore:
        _LOGGER.warning(
            "VLANMGR_LIVE_SKIP_RESTORE=1: initial-state representability check "
            "and cleanup restoration are disabled for switch %s; changes may "
            "remain on the device. Recovery files will be retained under %s",
            name,
            run_dir,
        )
    else:
        try:
            cli_config = _config_from_desired(target, original_desired)
            compiled = yaml.safe_load(_run_cli(target, "compile", cli_config))
            expected = _without_empty_parking_vlan(
                original_desired, target.parking_vid
            )
            if compiled != {name: expected}:
                _LOGGER.error(
                    "Initial switch state is not representable by generated CLI "
                    "intent; compiled state follows"
                )
                _log_state_summary(name, expected, label="expected")
                _log_state_summary(name, compiled[name], label="compiled")
                raise RuntimeError(
                    "The initial switch state cannot be represented exactly by "
                    "CLI intent; refusing live writes"
                )
        except Exception as error:
            if "compiled" not in locals():
                _LOGGER.error(
                    "Could not convert initial switch state to CLI intent: %s",
                    error,
                )
            _log_state_summary(name, original_desired, label="observed")
            shutil.rmtree(run_dir)
            raise

    _LOGGER.info("Live test CLI backups will be retained under %s", backup_dir)
    try:
        yield target
    finally:
        if skip_restore:
            _LOGGER.warning(
                "Skipped live switch restoration as requested; device changes "
                "and recovery files remain under %s",
                run_dir,
            )
        else:
            try:
                restored = _restore_test_scope(target)
                _assert_upstream_unchanged(restored, original)
            except Exception as exc:
                _LOGGER.exception(
                    "Live test cleanup failed; retained recovery files under %s",
                    run_dir,
                )
                pytest.fail(
                    f"Live test cleanup failed; recovery files retained under "
                    f"{run_dir}: {str(exc).replace(password, '<redacted>')}",
                    pytrace=False,
                )
            try:
                shutil.rmtree(run_dir)
            except OSError as exc:
                pytest.fail(
                    f"Live test state was restored, but temporary files could not "
                    f"be removed under {run_dir}: {exc}"
                )
            _LOGGER.info("Removed live test backups after successful cleanup")
            _LOGGER.info(
                "Live test VLAN/PVID scope restored on ports %s", _TEST_PORTS
            )


@pytest.mark.skipif(
    os.environ.get("VLANMGR_LIVE_TESTS") != "1",
    reason="set VLANMGR_LIVE_TESTS=1 to enable tests that write to a real switch",
)
def test_real_switch_vlan_membership_pvid_transitions_and_cleanup(
    live_switch: LiveSwitch,
) -> None:
    target = live_switch
    vlan_a, vlan_b = target.test_vids
    vlan_name_a, vlan_name_b = _available_test_vlan_names(
        target.original, target.test_vids
    )
    port_access, port_native, port_trunk = _TEST_PORTS
    desired = _desired_from_live(target.original)
    vlan_one = desired["vlans"].get(1)
    if vlan_one is None:
        pytest.fail("VLAN 1 must exist to test the upstream/access transition")

    desired["vlans"][vlan_a] = {
        "name": vlan_name_a,
        "tagged_ports": [1],
        "untagged_ports": [],
    }
    desired["vlans"][vlan_b] = {
        "name": vlan_name_b,
        "tagged_ports": [1],
        "untagged_ports": [],
    }
    desired["ports"][port_trunk]["pvid"] = target.parking_vid
    desired["vlans"].setdefault(
        target.parking_vid,
        {
            "name": _PARKING_VLAN_NAME,
            "tagged_ports": [],
            "untagged_ports": [],
        },
    )

    _set_port_memberships(desired, port_access, untagged=1)
    _set_port_memberships(
        desired, port_native, tagged=(vlan_a,), untagged=vlan_b
    )
    _set_port_memberships(
        desired,
        port_trunk,
        tagged=(vlan_a, vlan_b),
        untagged=target.parking_vid,
    )
    state = _apply_desired(target, desired)
    _assert_upstream_unchanged(
        state,
        target.original,
        parking_vid=target.parking_vid,
        allow_added_tagged=True,
    )
    assert state["vlans"][vlan_a]["tagged_ports"] == [
        1,
        port_native,
        port_trunk,
    ]
    assert state["vlans"][vlan_b]["tagged_ports"] == [1, port_trunk]
    assert state["vlans"][vlan_b]["untagged_ports"] == [port_native]
    assert state["ports"][port_native]["pvid"] == vlan_b

    desired = _desired_from_live(state)
    _set_port_memberships(desired, port_access, untagged=vlan_a)
    state = _apply_desired(target, desired)
    assert port_access not in state["vlans"][1]["untagged_ports"]
    assert port_access in state["vlans"][vlan_a]["untagged_ports"]
    assert state["ports"][port_access]["pvid"] == vlan_a

    desired = _desired_from_live(state)
    _set_port_memberships(desired, port_access, untagged=vlan_b)
    state = _apply_desired(target, desired)
    assert port_access not in state["vlans"][vlan_a]["untagged_ports"]
    assert port_access in state["vlans"][vlan_b]["untagged_ports"]
    assert state["ports"][port_access]["pvid"] == vlan_b

    restored = _restore_test_scope(target)
    assert vlan_a not in restored["vlans"]
    assert vlan_b not in restored["vlans"]
    original_effective = _desired_from_live(target.original)
    for port in _TEST_PORTS:
        assert (
            restored["ports"][port]["pvid"]
            == original_effective["ports"][port]["pvid"]
        )
        for vid, vlan in original_effective["vlans"].items():
            assert (port in restored["vlans"][vid]["tagged_ports"]) == (
                port in vlan["tagged_ports"]
            )
            assert (port in restored["vlans"][vid]["untagged_ports"]) == (
                port in vlan["untagged_ports"]
            )

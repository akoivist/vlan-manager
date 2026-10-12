import os
from importlib.metadata import version as package_version
from types import SimpleNamespace

import pytest

from vlanmgr import cli
from vlanmgr.adapters.tplink import collect_switch_baseline, read_switch_state
from vlanmgr.desired import compile_desired_state, validate_desired_state


class FakeSwitch:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_system_info(self):
        return SimpleNamespace(
            hardware="TL-SG108E 5.0",
            firmware="1.0.0 Build 20240101 Rel.12345",
            ip="192.168.1.10",
        )

    def get_port_settings(self):
        return [
            SimpleNamespace(
                port=port,
                enabled=port == 1,
                speed_cfg="Auto" if port == 1 else None,
                speed_act="1000M-Full" if port == 1 else None,
                fc_cfg=port == 1,
                fc_act=port == 1,
                trunk_id=0,
            )
            for port in range(1, 9)
        ]

    def get_pvids(self):
        return [1, 20, 1, 1, 1, 1, 1, 1]

    def get_dot1q_vlans(self):
        return True, [
            SimpleNamespace(
                vid=20,
                name="home",
                tagged_members=0b01,
                untagged_members=0b10,
            )
        ]


def test_collect_switch_baseline_normalizes_inventory():
    captured = {}

    def factory(host, *, username, password):
        captured.update(host=host, username=username, password=password)
        return FakeSwitch()

    result = read_switch_state(
        "core",
        {"host": "192.168.1.10", "model": "TL-SG108E"},
        "admin",
        "secret",
        switch_factory=factory,
    )

    assert captured == {
        "host": "192.168.1.10",
        "username": "admin",
        "password": "secret",
    }
    assert result["model"] == "TL-SG108E"
    assert result["hardware_revision"] == "5.0"
    assert result["firmware"] == "1.0.0 Build 20240101 Rel.12345"
    assert result["management_address"] == "192.168.1.10"
    assert result["port_count"] == 8
    assert result["vlans"] == {
        20: {
        "name": "home",
        "tagged_ports": [1],
        "untagged_ports": [2],
        }
    }
    assert result["ports"][1]["pvid"] == 1
    assert result["ports"][2]["pvid"] == 20
    assert result["ports"][1]["speed_actual"] == "1000M-Full"
    assert result["limitations"] == []


def test_read_switch_state_detects_supported_model_from_device():
    result = read_switch_state(
        "core",
        {"host": "192.168.1.10"},
        "admin",
        "secret",
        switch_factory=lambda *_args, **_kwargs: FakeSwitch(),
    )

    assert result["model"] == "TL-SG108E"


def test_sg1016de_vlan_one_membership_is_effective_only_when_pvid_is_one():
    class DEWithDefaultVlanOne(FakeSwitch):
        def get_system_info(self):
            info = super().get_system_info()
            info.hardware = "TL-SG1016DE 2.0"
            return info

        def get_port_settings(self):
            return [
                SimpleNamespace(
                    port=port,
                    enabled=True,
                    speed_cfg=None,
                    speed_act=None,
                    fc_cfg=False,
                    fc_act=False,
                    trunk_id=0,
                )
                for port in range(1, 17)
            ]

        def get_pvids(self):
            return [1, 2] + [1] * 14

        def get_dot1q_vlans(self):
            return True, [
                SimpleNamespace(
                    vid=1,
                    name="default",
                    tagged_members=0,
                    untagged_members=0xffff,
                ),
                SimpleNamespace(
                    vid=2,
                    name="home",
                    tagged_members=0,
                    untagged_members=0b10,
                ),
            ]

    result = read_switch_state(
        "edge",
        {"host": "192.168.1.12"},
        "admin",
        "secret",
        switch_factory=lambda *_args, **_kwargs: DEWithDefaultVlanOne(),
    )

    assert result["vlans"][1]["untagged_ports"] == [
        1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
    ]
    assert result["vlans"][2]["untagged_ports"] == [2]
    assert "retains VLAN 1 as an untagged member" in result["limitations"][0]


def test_read_switch_state_preserves_multiple_untagged_memberships_for_repair():
    class ConflictingMemberships(FakeSwitch):
        def get_dot1q_vlans(self):
            return True, [
                SimpleNamespace(
                    vid=1,
                    name="management",
                    tagged_members=0,
                    untagged_members=0b10,
                ),
                SimpleNamespace(
                    vid=20,
                    name="home",
                    tagged_members=0,
                    untagged_members=0b10,
                ),
            ]

    result = read_switch_state(
        "core",
        {"host": "192.168.1.10"},
        "admin",
        "secret",
        switch_factory=lambda *_args, **_kwargs: ConflictingMemberships(),
    )
    assert result["vlans"][1]["untagged_ports"] == [2]
    assert result["vlans"][20]["untagged_ports"] == [2]


def test_login_failure_suggests_switch_password_environment_variable():
    class LoginFailure:
        def __enter__(self):
            raise RuntimeError(
                "Login failed (errType=1). Check username and password."
            )

        def __exit__(self, *_args):
            return False

    with pytest.raises(RuntimeError) as error:
        read_switch_state(
            "living room-switch",
            {"host": "192.168.1.10"},
            "admin",
            "wrong",
            switch_factory=lambda *_args, **_kwargs: LoginFailure(),
        )

    assert "TPLINK_PASSWORD_LIVING_ROOM_SWITCH" in str(error.value)
    assert "TPLINK_PASSWORD as the shared fallback" in str(error.value)
    assert "wrong" not in str(error.value)


def test_configured_model_must_be_verified_before_connecting():
    factory_called = False

    def factory(*_args, **_kwargs):
        nonlocal factory_called
        factory_called = True
        return FakeSwitch()

    with pytest.raises(RuntimeError, match="unsupported or unverified"):
        collect_switch_baseline(
            "unknown",
            {"host": "192.0.2.1", "model": "TL-SG108PE"},
            "admin",
            "secret",
            switch_factory=factory,
        )
    assert not factory_called


def test_reported_port_count_must_match_supported_model():
    class IncompleteSwitch(FakeSwitch):
        def get_port_settings(self):
            return super().get_port_settings()[:-1]

    with pytest.raises(RuntimeError, match="expected 8"):
        collect_switch_baseline(
            "core",
            {"host": "192.168.1.10", "model": "TL-SG108E"},
            "admin",
            "secret",
            switch_factory=lambda *_args, **_kwargs: IncompleteSwitch(),
        )


def test_baseline_command_writes_minimal_live_vlan_summary(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config = _valid_config()
    config["router"] = "not a desired-state mapping"
    config_path.write_text(cli.yaml.safe_dump(config), encoding="utf-8")
    output_dir = tmp_path / "snapshots"
    monkeypatch.setenv("TPLINK_PASSWORD", "secret")
    collected = {}

    def collect(switches_config, _username, _passwords):
        collected.update(switches_config)
        return {
            "core": {
                "vlans": {
                    20: {
                        "name": "home",
                        "tagged_ports": [1],
                        "untagged_ports": [2],
                    },
                    30: {
                        "name": "iot",
                        "tagged_ports": [],
                        "untagged_ports": [1],
                    },
                },
                "ports": {
                    1: {"pvid": 30},
                    2: {"pvid": 20},
                },
            },
        }

    monkeypatch.setattr(
        cli,
        "_collect",
        collect,
    )

    assert cli.main([
        "baseline",
        "--config", str(config_path),
        "--switch", "core",
        "--output-dir", str(output_dir),
    ]) == 0

    snapshot_files = list(output_dir.glob("baseline-*.yaml"))
    assert len(snapshot_files) == 1
    snapshot = cli.yaml.safe_load(snapshot_files[0].read_text(encoding="utf-8"))
    assert collected == {
        "core": {"host": "192.168.1.10"},
    }
    assert snapshot == {
        "vlans": {
            "home": {"id": 20, "name": "home"},
            "iot": {"id": 30, "name": "iot"},
        },
        "switches": {
            "core": {
                "host": "192.168.1.10",
                "ports": {
                    1: {
                        "kind": "trunk",
                        "vlans": ["home", "iot"],
                        "native_vlan": "iot",
                    },
                    2: {
                        "kind": "access",
                        "native_vlan": "home",
                    },
                },
            },
        },
    }
    assert list(snapshot) == ["vlans", "switches"]
    assert set(snapshot["switches"]) == {"core"}


def test_unknown_switch_is_rejected_before_reading_device(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(cli.yaml.safe_dump(_valid_config()), encoding="utf-8")
    read_called = False

    def read(*_args):
        nonlocal read_called
        read_called = True
        raise AssertionError("must not connect to a device")

    monkeypatch.setattr(cli, "_read_live_switches", read)
    monkeypatch.setenv("TPLINK_PASSWORD", "secret")

    assert cli.main([
        "plan", "--config", str(config_path), "--switch", "missing"
    ]) == 1
    assert "Unknown switch 'missing'" in capsys.readouterr().err
    assert not read_called


def test_plan_reads_and_reports_only_selected_switch(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config = _valid_config()
    config_path.write_text(cli.yaml.safe_dump(config), encoding="utf-8")
    selected = {}
    desired = compile_desired_state(config)

    def read(config_subset, _username, _passwords):
        selected.update(config_subset["switches"])
        switch = next(iter(config_subset["switches"]))
        return (
            {
                switch: {
                    "vlans": desired[switch]["vlans"],
                    "ports": desired[switch]["ports"],
                    "dot1q_enabled": True,
                }
            },
            {switch: {
                "add_dot1q_vlan", "delete_dot1q_vlan",
                "set_pvid", "save_config", "backup_config",
            }},
        )

    monkeypatch.setattr(cli, "_read_live_switches", read)
    monkeypatch.setenv("TPLINK_PASSWORD", "secret")

    assert cli.main([
        "plan", "--config", str(config_path), "--switch", "switch2"
    ]) == 0
    assert set(selected) == {"switch2"}
    output = capsys.readouterr().out
    assert "switch2:" in output
    assert "switch1:" not in output


def test_baseline_summary_defines_unconfigured_live_vlan_and_pvid():
    summary = cli._baseline_summary(
        {"switches": {"switch1": {"host": "192.0.2.1"}}},
        {
            "switch1": {
                "vlans": {
                    70: {
                        "name": "Guest Network",
                        "tagged_ports": [1],
                        "untagged_ports": [],
                    },
                },
                "ports": {
                    1: {"pvid": 99},
                },
            },
        },
    )

    assert summary["vlans"] == {
        "guest_network": {"id": 70, "name": "Guest Network"},
        "vlan99": {"id": 99, "name": "VLAN99"},
    }
    assert summary["switches"]["switch1"]["ports"][1] == {
        "kind": "trunk",
        "vlans": ["guest_network"],
        "native_vlan": "vlan99",
    }


def test_baseline_summary_omits_vlan_list_for_access_ports():
    summary = cli._baseline_summary(
        {"switches": {"switch1": {"host": "192.0.2.1"}}},
        {
            "switch1": {
                "vlans": {
                    1: {
                        "name": "management",
                        "tagged_ports": [],
                        "untagged_ports": [1],
                    },
                },
                "ports": {1: {"pvid": 1}},
            },
        },
    )

    assert summary["switches"]["switch1"]["ports"][1] == {
        "kind": "access",
        "native_vlan": "management",
    }


def test_semantic_diff_reports_vlan_changes_memberships_and_pvids():
    desired = {
        "switch1": {
            "vlans": {
                1: {"name": "management", "tagged_ports": [], "untagged_ports": [1]},
                20: {"name": "home", "tagged_ports": [1], "untagged_ports": [2]},
                30: {"name": "iot", "tagged_ports": [], "untagged_ports": [3]},
            },
            "ports": {1: {"pvid": 1}, 2: {"pvid": 20}, 3: {"pvid": 30}},
        }
    }
    actual = {
        "switch1": {
            "dot1q_enabled": True,
            "vlans": {
                1: {"name": "management", "tagged_ports": [], "untagged_ports": [1]},
                20: {"name": "old-home", "tagged_ports": [], "untagged_ports": [2]},
                40: {"name": "extra", "tagged_ports": [3], "untagged_ports": []},
            },
            "ports": {1: {"pvid": 1}, 2: {"pvid": 20}, 3: {"pvid": 40}},
        }
    }

    plan = cli.semantic_diff(
        desired,
        actual,
        {"switch1": {
            "add_dot1q_vlan", "delete_dot1q_vlan", "set_pvid",
            "save_config", "backup_config",
        }},
    )["switch1"]

    assert [vlan["vid"] for vlan in plan["vlans"]["create"]] == [30]
    assert [change["vid"] for change in plan["vlans"]["update"]] == [20]
    assert [vlan["vid"] for vlan in plan["vlans"]["delete"]] == [40]
    assert {"port": 1, "vid": 20, "from": "none", "to": "tagged"} in plan["memberships"]
    assert {"port": 3, "vid": 40, "from": "tagged", "to": "none"} in plan["memberships"]
    assert plan["pvids"] == [{"port": 3, "from": 40, "to": 30}]


def test_semantic_diff_blocks_vlan_changes_when_8021q_is_disabled():
    desired = {
        "switch1": {
            "vlans": {
                20: {"name": "home", "tagged_ports": [], "untagged_ports": [1]},
            },
            "ports": {1: {"pvid": 20}},
        }
    }
    actual = {
        "switch1": {
            "dot1q_enabled": False,
            "vlans": {},
            "ports": {1: {"pvid": 1}},
        }
    }

    plan = cli.semantic_diff(
        desired,
        actual,
        {"switch1": {"add_dot1q_vlan", "set_pvid", "save_config"}},
    )["switch1"]
    assert "dot1q" not in plan
    assert any(
        "will not change it" in message
        for message in plan["unsupported_behavior"]
    )


def test_semantic_diff_warns_when_backup_is_unsupported():
    desired = {
        "switch1": {
            "vlans": {20: {"name": "home", "tagged_ports": [], "untagged_ports": []}},
            "ports": {},
        }
    }
    actual = {
        "switch1": {
            "dot1q_enabled": True,
            "vlans": {},
            "ports": {},
        }
    }

    plan = cli.semantic_diff(desired, actual, {"switch1": {"add_dot1q_vlan"}})
    assert "backup" in plan["switch1"]["warnings"][0]


def test_apply_adapter_backs_up_writes_saves_and_verifies(tmp_path):
    class WritableFakeSwitch(FakeSwitch):
        def __init__(self):
            self.vlans = {
                20: SimpleNamespace(
                    vid=20,
                    name="home",
                    tagged_members=0,
                    untagged_members=0b10,
                )
            }
            self.pvids = [1, 20, 1, 1, 1, 1, 1, 1]
            self.events = []

        def get_pvids(self):
            return list(self.pvids)

        def get_dot1q_vlans(self):
            return True, list(self.vlans.values())

        def backup_config(self):
            self.events.append("backup")
            return b"fake-backup"

        def add_dot1q_vlan(self, vid, name, *, tagged_ports, untagged_ports):
            self.events.append(("add", vid))
            self.vlans[vid] = SimpleNamespace(
                vid=vid,
                name=name,
                tagged_members=sum(1 << (port - 1) for port in tagged_ports),
                untagged_members=sum(1 << (port - 1) for port in untagged_ports),
            )

        def delete_dot1q_vlan(self, vid):
            self.events.append(("delete", vid))
            del self.vlans[vid]

        def set_pvid(self, ports, pvid):
            self.events.append(("pvid", tuple(ports), pvid))
            for port in ports:
                self.pvids[port - 1] = pvid

        def set_dot1q_enabled(self, enabled):
            self.events.append(("dot1q", enabled))

        def save_config(self):
            self.events.append("save")

    device = WritableFakeSwitch()
    desired_switch = {
        "vlans": {
            20: {"name": "home", "tagged_ports": [], "untagged_ports": [2]},
            30: {"name": "iot", "tagged_ports": [1], "untagged_ports": [3]},
        },
        "ports": {n: {"pvid": 1} for n in range(1, 9)},
    }
    desired_switch["ports"][2]["pvid"] = 20
    desired_switch["ports"][3]["pvid"] = 30
    config = {"host": "192.168.1.10", "model": "TL-SG108E"}
    actual = read_switch_state(
        "core", config, "admin", "secret",
        switch_factory=lambda *_args, **_kwargs: device,
    )

    verified = cli.apply_switch_plan(
        name="core",
        config=config,
        desired=desired_switch,
        approved_actual=actual,
        username="admin",
        password="secret",
        backup_path=tmp_path / "backups/core.bin",
        switch_factory=lambda *_args, **_kwargs: device,
    )

    assert (tmp_path / "backups/core.bin").read_bytes() == b"fake-backup"
    if os.name == "posix":
        assert (tmp_path / "backups/core.bin").stat().st_mode & 0o777 == 0o600
    assert device.events[0] == "backup"
    assert device.events[-1] == "save"
    assert not any(
        isinstance(event, tuple) and event[0] == "dot1q"
        for event in device.events
    )
    assert verified["vlans"][30]["untagged_ports"] == [3]
    assert verified["ports"][3]["pvid"] == 30


def test_apply_removes_old_untagged_membership_before_setting_target(tmp_path):
    class DuplicateMembershipSwitch(FakeSwitch):
        def __init__(self):
            self.pvids = [1] * 8
            self.vlans = {
                1: SimpleNamespace(
                    vid=1,
                    name="management",
                    tagged_members=0,
                    untagged_members=0xff,
                ),
                2: SimpleNamespace(
                    vid=2,
                    name="home",
                    tagged_members=0,
                    untagged_members=0,
                ),
            }
            self.events = []

        def get_pvids(self):
            return list(self.pvids)

        def get_dot1q_vlans(self):
            return True, list(self.vlans.values())

        def backup_config(self):
            return b"fake-backup"

        def add_dot1q_vlan(self, vid, name, *, tagged_ports, untagged_ports):
            new_untagged = set(untagged_ports)
            self.events.append(("vlan", vid, tuple(sorted(new_untagged))))
            if (
                vid == 1
                and any(
                    port not in new_untagged and pvid == 1
                    for port, pvid in enumerate(self.pvids, start=1)
                )
            ):
                new_untagged.update(
                    port for port, pvid in enumerate(self.pvids, start=1)
                    if pvid == 1
                )
            self.vlans[vid] = SimpleNamespace(
                vid=vid,
                name=name,
                tagged_members=sum(1 << (port - 1) for port in tagged_ports),
                untagged_members=sum(1 << (port - 1) for port in new_untagged),
            )

        def set_pvid(self, ports, pvid):
            self.events.append(("pvid", tuple(ports), pvid))
            for port in ports:
                self.pvids[port - 1] = pvid

        def save_config(self):
            self.events.append(("save",))

    device = DuplicateMembershipSwitch()
    config = {"host": "192.168.1.10", "model": "TL-SG108E"}
    factory = lambda *_args, **_kwargs: device
    actual = read_switch_state(
        "core", config, "admin", "secret", switch_factory=factory
    )
    desired = {
        "vlans": {
            1: {
                "name": "management",
                "tagged_ports": [],
                "untagged_ports": [1, 2, 3, 4, 6, 7, 8],
            },
            2: {
                "name": "home",
                "tagged_ports": [],
                "untagged_ports": [5],
            },
        },
        "ports": {
            port: {"pvid": 2 if port == 5 else 1}
            for port in range(1, 9)
        },
    }

    verified = cli.apply_switch_plan(
        name="core",
        config=config,
        desired=desired,
        approved_actual=actual,
        username="admin",
        password="secret",
        backup_path=tmp_path / "backups/core.bin",
        switch_factory=factory,
    )

    remove_vlan_one = device.events.index(
        ("vlan", 1, (1, 2, 3, 4, 6, 7, 8))
    )
    destination_write = device.events.index(("vlan", 2, (5,)))
    pvid_write = device.events.index(("pvid", (5,), 2))
    assert destination_write < pvid_write < remove_vlan_one
    assert 5 not in verified["vlans"][1]["untagged_ports"]
    assert verified["vlans"][2]["untagged_ports"] == [5]
    assert verified["ports"][5]["pvid"] == 2


def test_apply_reconciles_vlan_one_reintroduced_by_pvid_change(tmp_path):
    class ReintroducesVlanOneSwitch(FakeSwitch):
        def __init__(self):
            self.pvids = [1, 1, 1, 1, 2, 1, 1, 1]
            self.vlans = {
                1: SimpleNamespace(
                    vid=1,
                    name="management",
                    tagged_members=0,
                    untagged_members=0b11101111,
                ),
                2: SimpleNamespace(
                    vid=2,
                    name="home",
                    tagged_members=0,
                    untagged_members=1 << 4,
                ),
            }
            self.events = []

        def get_pvids(self):
            return list(self.pvids)

        def get_dot1q_vlans(self):
            return True, list(self.vlans.values())

        def backup_config(self):
            return b"fake-backup"

        def add_dot1q_vlan(self, vid, name, *, tagged_ports, untagged_ports):
            self.events.append(("vlan", vid, tuple(sorted(untagged_ports))))
            self.vlans[vid] = SimpleNamespace(
                vid=vid,
                name=name,
                tagged_members=sum(1 << (port - 1) for port in tagged_ports),
                untagged_members=sum(
                    1 << (port - 1) for port in untagged_ports
                ),
            )

        def set_pvid(self, ports, pvid):
            self.events.append(("pvid", tuple(ports), pvid))
            for port in ports:
                self.pvids[port - 1] = pvid
                self.vlans[1].untagged_members |= 1 << (port - 1)

        def save_config(self):
            self.events.append(("save",))

    device = ReintroducesVlanOneSwitch()
    config = {"host": "192.168.1.10", "model": "TL-SG108E"}
    factory = lambda *_args, **_kwargs: device
    actual = read_switch_state(
        "core", config, "admin", "secret", switch_factory=factory
    )
    desired = {
        "vlans": {
            1: {
                "name": "management",
                "tagged_ports": [],
                "untagged_ports": [1, 2, 3, 4, 6, 7, 8],
            },
            2: {"name": "home", "tagged_ports": [], "untagged_ports": []},
            3: {"name": "iot", "tagged_ports": [], "untagged_ports": [5]},
        },
        "ports": {
            port: {"pvid": 3 if port == 5 else actual["ports"][port]["pvid"]}
            for port in range(1, 9)
        },
    }

    verified = cli.apply_switch_plan(
        name="core",
        config=config,
        desired=desired,
        approved_actual=actual,
        username="admin",
        password="secret",
        backup_path=tmp_path / "backups/core.bin",
        switch_factory=factory,
    )

    pvid_write = device.events.index(("pvid", (5,), 3))
    vlan_one_write = device.events.index(
        ("vlan", 1, (1, 2, 3, 4, 6, 7, 8))
    )
    assert pvid_write < vlan_one_write
    assert 5 not in verified["vlans"][1]["untagged_ports"]
    assert verified["vlans"][3]["untagged_ports"] == [5]
    assert verified["ports"][5]["pvid"] == 3


def test_apply_logs_and_identifies_sdk_write_failure(tmp_path, caplog):
    class FailingWriteSwitch(FakeSwitch):
        def backup_config(self):
            return b"fake-backup"

        def add_dot1q_vlan(self, *_args, **_kwargs):
            raise OSError("switch rejected VLAN request")

        def set_pvid(self, *_args, **_kwargs):
            pass

        def save_config(self):
            pass

    device = FailingWriteSwitch()
    config = {"host": "192.168.1.10", "model": "TL-SG108E"}
    factory = lambda *_args, **_kwargs: device
    actual = read_switch_state(
        "core", config, "admin", "secret", switch_factory=factory
    )
    desired = {
        "vlans": {
            20: {"name": "home", "tagged_ports": [], "untagged_ports": [3]},
        },
        "ports": {
            port: {"pvid": 20 if port == 3 else actual["ports"][port]["pvid"]}
            for port in range(1, 9)
        },
    }

    with caplog.at_level("INFO"):
        with pytest.raises(
            RuntimeError,
            match=(
                r"SDK operation add_dot1q_vlan vid=20 "
                r"\(remove old untagged membership\) failed"
            ),
        ) as error:
            cli.apply_switch_plan(
                name="core",
                config=config,
                desired=desired,
                approved_actual=actual,
                username="admin",
                password="secret",
                backup_path=tmp_path / "backups/core.bin",
                switch_factory=factory,
            )

    assert isinstance(error.value.__cause__, OSError)
    assert (
        "SDK operation add_dot1q_vlan vid=20 "
        "(remove old untagged membership) starting"
    ) in caplog.text
    assert (
        "SDK operation add_dot1q_vlan vid=20 "
        "(remove old untagged membership) failed"
    ) in caplog.text


def test_apply_command_requires_confirmation_before_adapter_apply(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(cli.yaml.safe_dump(_valid_config()), encoding="utf-8")
    desired = compile_desired_state(_valid_config())
    actual = {
        name: {
            "vlans": {},
            "ports": {port: {"pvid": 998} for port in switch["ports"]},
            "dot1q_enabled": True,
        }
        for name, switch in desired.items()
    }
    monkeypatch.setattr(
        cli, "_read_live_switches",
        lambda *_args: (
            actual,
            {name: {"add_dot1q_vlan", "delete_dot1q_vlan", "set_pvid",
                    "save_config"} for name in desired},
        ),
    )
    apply_called = False

    def apply(*_args, **_kwargs):
        nonlocal apply_called
        apply_called = True

    monkeypatch.setattr(cli, "apply_switch_plan", apply)
    monkeypatch.setenv("TPLINK_PASSWORD", "secret")
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert cli.main(["apply", "--config", str(config_path)]) == 1
    assert "cancelled" in capsys.readouterr().out
    assert not apply_called


def test_passwords_are_selected_per_switch_with_shared_fallback(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "switches:\n"
        "  core:\n"
        "    host: 192.168.1.10\n"
        "    model: TL-SG108E\n"
        "  switch2:\n"
        "    host: 192.168.1.11\n"
        "    model: TL-SG108E\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TPLINK_PASSWORD", "shared-secret")
    monkeypatch.setenv("TPLINK_PASSWORD_CORE", "core-secret")
    monkeypatch.delenv("TPLINK_PASSWORD_SWITCH2", raising=False)

    config = cli._load_config(config_path)
    passwords = cli._switch_passwords(config)
    assert passwords == {
        "core": "core-secret",
        "switch2": "shared-secret",
    }
    received_passwords = {}

    def collect(name, _switch_config, *, username, password):
        received_passwords[name] = (username, password)
        return {"name": name}

    monkeypatch.setattr(cli, "collect_switch_baseline", collect)
    assert cli._collect(cli._baseline_switch_config(config), "admin", passwords) == {
        "core": {"name": "core"},
        "switch2": {"name": "switch2"},
    }
    assert received_passwords == {
        "core": ("admin", "core-secret"),
        "switch2": ("admin", "shared-secret"),
    }


def test_missing_password_reports_per_switch_environment_variable(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(cli.yaml.safe_dump(_valid_config()), encoding="utf-8")
    monkeypatch.delenv("TPLINK_PASSWORD", raising=False)
    monkeypatch.delenv("TPLINK_PASSWORD_CORE", raising=False)

    assert cli.main(["baseline", "--config", str(config_path)]) == 1
    assert "TPLINK_PASSWORD_CORE" in capsys.readouterr().err


def _valid_config():
    return {
        "version": 1,
        "router": {"name": "udm"},
        "vlans": {
            "management": {"id": 1, "name": "management"},
            "home": {"id": 20, "name": "home"},
            "iot": {"id": 30, "name": "iot"},
            "parking": {"id": 998, "name": "parking"},
        },
        "wifi_networks": {
            "home_wifi": {"ssid": "Home", "vlan": "home"},
            "guest_wifi": {"ssid": "Guest", "vlan": "iot"},
        },
        "switches": {
            "core": {
                "host": "192.168.1.10",
                "model": "TL-SG108E",
                "port_count": 8,
                "ports": {
                    1: {
                        "kind": "link",
                        "name": "udm-link",
                        "upstream": "router",
                        "native_vlan": "management",
                    },
                    2: {
                        "kind": "access",
                        "name": "endpoint",
                        "native_vlan": "iot",
                    },
                    3: {
                        "kind": "trunk",
                        "name": "server",
                        "vlans": ["management", "home", "iot"],
                        "native_vlan": "management",
                    },
                    4: {"kind": "wifi", "native_vlan": "management"},
                    8: {
                        "kind": "link",
                        "downstream": "switch2",
                        "native_vlan": "management",
                    },
                },
            },
            "switch2": {
                "host": "192.168.1.11",
                "model": "TL-SG108E",
                "port_count": 8,
                "ports": {
                    1: {"kind": "wifi", "native_vlan": "management"},
                    8: {
                        "kind": "link",
                        "upstream": "core",
                        "native_vlan": "management",
                    },
                },
            },
        },
    }


def test_validate_desired_state_accepts_complete_reciprocal_topology():
    validate_desired_state(_valid_config())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda config: config["vlans"]["iot"].update(id=20), "assigned to both"),
        (lambda config: config["vlans"]["iot"].update(id=4095), "1 to 4094"),
        (lambda config: config["vlans"]["iot"].update(name="home"),
         "VLAN name .* is shared"),
        (lambda config: config["wifi_networks"]["guest_wifi"].update(vlan="missing"),
         "unknown VLAN"),
        (lambda config: config["switches"]["core"].update(port_count=7),
         "port_count must be 8"),
        (lambda config: config["switches"]["core"]["ports"][1].update(
            native_vlan="home"
        ), "link must use VLAN 1"),
        (lambda config: config["switches"]["core"]["ports"][8].update(
            tagged_vlans=["home"]
        ), "only on a router-facing link"),
        (lambda config: config["switches"]["core"]["ports"][1].update(
            tagged_vlans=["management"]
        ), "must not include its native VLAN"),
        (lambda config: config["switches"]["core"]["ports"][1].update(
            tagged_vlans=["parking"]
        ), "must not reference parking"),
        (lambda config: config["switches"]["core"]["ports"][2].update(
            native_vlan="missing"
        ), "unknown VLAN"),
        (lambda config: config["switches"]["switch2"]["ports"][8].update(
            native_vlan="home"
        ), "mismatched native VLANs"),
        (lambda config: config["switches"]["switch2"]["ports"][8].pop("native_vlan"),
         "native_vlan"),
        (lambda config: config["switches"]["core"]["ports"][8].update(
            downstream="missing"
        ), "unknown downstream switch"),
        (lambda config: config["switches"]["core"]["ports"][2].update(kind="console"),
         "kind must be"),
    ],
)
def test_invalid_desired_state_fails_closed(mutate, message):
    config = _valid_config()
    mutate(config)
    with pytest.raises(RuntimeError, match=message):
        validate_desired_state(config)


def test_compile_intents_propagates_vlan_requirements_and_pvids():
    result = compile_desired_state(_valid_config())

    core = result["core"]
    switch2 = result["switch2"]
    assert core["ports"][2]["pvid"] == 30
    assert core["vlans"][30]["untagged_ports"] == [2]
    assert core["vlans"][30]["tagged_ports"] == [1, 3, 4, 8]
    assert core["vlans"][20]["tagged_ports"] == [1, 3, 4, 8]
    assert core["vlans"][1]["untagged_ports"] == [1, 3, 4, 8]
    assert switch2["ports"][1]["pvid"] == 1
    assert switch2["vlans"][20]["tagged_ports"] == [1, 8]
    assert switch2["vlans"][30]["tagged_ports"] == [1, 8]
    assert switch2["vlans"][1]["untagged_ports"] == [1, 8]
    for switch_name, switch in _valid_config()["switches"].items():
        for port_number, port_config in switch["ports"].items():
            if port_config["kind"] == "link":
                assert result[switch_name]["ports"][port_number]["pvid"] == 1
                assert port_number in result[switch_name]["vlans"][1]["untagged_ports"]
    assert core["ports"][5]["pvid"] == 998
    assert switch2["ports"][5]["pvid"] == 998
    for switch_state in (core, switch2):
        assert 998 not in switch_state["vlans"]
    for switch_state in result.values():
        for port_number, port_state in switch_state["ports"].items():
            untagged = [
                vid
                for vid, vlan in switch_state["vlans"].items()
                if port_number in vlan["untagged_ports"]
            ]
            assert len(untagged) <= 1
            if untagged:
                assert untagged[0] == port_state["pvid"]


def test_router_link_preserves_explicit_tagged_vlan_without_local_membership():
    config = _valid_config()
    config["vlans"]["routed_only"] = {"id": 40, "name": "routed-only"}
    config["switches"]["core"]["ports"][1]["tagged_vlans"] = ["routed_only"]

    result = compile_desired_state(config)

    assert result["core"]["vlans"][40]["tagged_ports"] == [1]
    assert 40 not in result["switch2"]["vlans"]


def test_topology_accepts_multiple_switches_directly_connected_to_router():
    config = _valid_config()
    config["vlans"]["camera"] = {"id": 40, "name": "camera"}
    config["switches"]["edge"] = {
        "host": "192.168.1.12",
        "model": "TL-SG108E",
        "port_count": 8,
        "ports": {
            1: {
                "kind": "link",
                "upstream": "router",
                "native_vlan": "management",
            },
            2: {"kind": "access", "native_vlan": "camera"},
        },
    }

    result = compile_desired_state(config)

    assert result["edge"]["vlans"][40]["untagged_ports"] == [2]
    assert result["edge"]["vlans"][40]["tagged_ports"] == [1]
    assert 40 not in result["core"]["vlans"]


def test_trunk_without_native_vlan_uses_parking_as_untagged_and_pvid():
    config = _valid_config()
    config["switches"]["core"]["ports"][3].pop("native_vlan")

    result = compile_desired_state(config)
    trunk = result["core"]
    assert trunk["ports"][3]["pvid"] == 998
    assert trunk["vlans"][1]["tagged_ports"].count(3) == 1
    assert trunk["vlans"][20]["tagged_ports"].count(3) == 1
    assert trunk["vlans"][30]["tagged_ports"].count(3) == 1
    assert trunk["vlans"][998]["untagged_ports"] == [3]
    assert trunk["vlans"][998]["tagged_ports"] == []
    assert 998 not in result["switch2"]["vlans"]


def test_parking_vlan_stays_local_to_a_trunk_and_is_not_propagated():
    config = _valid_config()
    config["switches"]["switch2"]["ports"][1] = {
        "kind": "trunk",
        "vlans": ["home"],
    }

    result = compile_desired_state(config)

    assert result["switch2"]["vlans"][998]["untagged_ports"] == [1]
    assert result["switch2"]["ports"][1]["pvid"] == 998
    assert 998 not in result["core"]["vlans"]
    assert result["core"]["ports"][8]["pvid"] == 1
    assert result["switch2"]["ports"][8]["pvid"] == 1
    assert result["switch2"]["vlans"][998]["tagged_ports"] == []
    assert result["switch2"]["vlans"][998]["untagged_ports"] == [1]


def test_parking_vlan_is_created_with_trunk_membership_and_pvid():
    config = _valid_config()
    config["switches"]["core"]["ports"][3].pop("native_vlan")
    desired = compile_desired_state(config)["core"]
    actual_vlans = {
        vid: vlan for vid, vlan in desired["vlans"].items() if vid != 998
    }
    actual = {
        "core": {
            "dot1q_enabled": True,
            "vlans": actual_vlans,
            "ports": {
                port: {"pvid": 1}
                for port in desired["ports"]
            },
        }
    }

    plan = cli.semantic_diff(
        {"core": desired},
        actual,
        {"core": {"add_dot1q_vlan", "set_pvid", "save_config"}},
    )["core"]

    assert plan["vlans"]["create"] == [{"vid": 998, **desired["vlans"][998]}]
    assert {
        "port": 3,
        "vid": 998,
        "from": "none",
        "to": "untagged",
    } in plan["memberships"]
    assert {"port": 3, "from": 1, "to": 998} in plan["pvids"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config["switches"]["core"]["ports"][2].update(
            native_vlan="parking"
        ),
        lambda config: config["switches"]["core"]["ports"][3]["vlans"].append(
            "parking"
        ),
        lambda config: config["switches"]["core"]["ports"][3].update(
            native_vlan="parking"
        ),
        lambda config: config["wifi_networks"]["home_wifi"].update(
            vlan="parking"
        ),
    ],
)
def test_parking_vlan_is_reserved_for_fallback_pvids(mutate):
    config = _valid_config()
    mutate(config)

    with pytest.raises(RuntimeError, match="must not reference parking"):
        validate_desired_state(config)


def test_validate_rejects_topology_loop():
    config = _valid_config()
    config["switches"]["core"]["ports"][7] = {
        "kind": "link",
        "downstream": "switch2",
        "native_vlan": "management",
    }
    config["switches"]["switch2"]["ports"][7] = {
        "kind": "link",
        "upstream": "core",
        "native_vlan": "management",
    }
    with pytest.raises(RuntimeError, match="exactly one reciprocal"):
        validate_desired_state(config)


def test_validate_command_checks_full_desired_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(cli.yaml.safe_dump(_valid_config()), encoding="utf-8")

    assert cli.main(["validate", "--config", str(config_path)]) == 0
    assert "valid" in capsys.readouterr().out


def test_config_option_is_required_for_each_command(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(cli.yaml.safe_dump(_valid_config()), encoding="utf-8")
    for command in ("validate", "compile", "baseline"):
        with pytest.raises(SystemExit) as error:
            cli.main([command])
        assert error.value.code == 2
        assert "--config" in capsys.readouterr().err


def test_cli_prints_help_without_a_command(capsys):
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: vlanmgr [-h] [--version] command ..." in output
    assert "commands:" in output
    assert "baseline --config PATH [--switch NAME] [--output-dir DIR]" in output
    assert "apply --config PATH [--switch NAME] [--backup-dir DIR]" in output


def test_cli_prints_package_version(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out == f"vlanmgr {package_version('vlan-manager')}\n"


def test_cli_prints_help_for_unknown_command(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["unknown"])
    assert error.value.code == 2
    assert "usage:" in capsys.readouterr().err
"""Command-line entry point for home VLAN manager."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import yaml

from vlanmgr.adapters.tplink import (
    apply_switch_plan,
    backup_switch_config,
    collect_switch_baseline,
    read_switch_state,
    switch_capabilities,
)
from vlanmgr.desired import compile_desired_state, validate_desired_state
from vlanmgr.reconcile import has_changes, semantic_diff


_LOGGER = logging.getLogger(__name__)


class _HelpOnErrorParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_help(sys.stderr)
        super().error(message)


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict) or not isinstance(config.get("switches"), dict):
        raise RuntimeError(f"{path}: expected a mapping with a switches section")
    if not config["switches"]:
        raise RuntimeError(f"{path}: no switches are configured")
    return config


def _password_env_name(switch_name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", switch_name).strip("_").upper()
    if not suffix:
        raise RuntimeError(
            f"Switch name {switch_name!r} cannot be used to derive a password "
            "environment variable"
        )
    return f"TPLINK_PASSWORD_{suffix}"


def _switch_passwords(config: dict[str, Any]) -> dict[str, str]:
    passwords = {}
    env_names: dict[str, str] = {}
    fallback = os.environ.get("TPLINK_PASSWORD")
    for name in config["switches"]:
        env_name = _password_env_name(name)
        previous_name = env_names.get(env_name)
        if previous_name is not None:
            raise RuntimeError(
                f"Switch names {previous_name!r} and {name!r} map to the same "
                f"password environment variable {env_name}"
            )
        env_names[env_name] = name

        password = os.environ.get(env_name)
        if password is None:
            password = fallback
        if not password:
            raise RuntimeError(
                f"Set {env_name} (or the shared fallback TPLINK_PASSWORD) "
                f"in the environment for switch {name!r}"
            )
        passwords[name] = password
    return passwords


def _baseline_switch_config(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    switches = config.get("switches")
    if not isinstance(switches, dict) or not switches:
        raise RuntimeError("Configuration must contain a non-empty switches mapping")

    result = {}
    for name, switch in switches.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"Invalid switch name {name!r}")
        if not isinstance(switch, dict):
            raise RuntimeError(f"Switch {name!r} configuration must be a mapping")
        host = switch.get("host")
        if not isinstance(host, str) or not host.strip():
            raise RuntimeError(f"Switch {name!r} has no management address")
        result[name] = {"host": host}
    return result


def _select_config_switches(
    config: dict[str, Any], switch_name: str | None
) -> dict[str, Any]:
    switches = config.get("switches")
    if not isinstance(switches, dict) or not switches:
        raise RuntimeError("Configuration must contain a non-empty switches mapping")
    if switch_name is not None and switch_name not in switches:
        raise RuntimeError(
            f"Unknown switch {switch_name!r}; configured switches: "
            f"{', '.join(switches)}"
        )
    selected = dict(config)
    selected["switches"] = (
        {switch_name: switches[switch_name]}
        if switch_name is not None
        else switches
    )
    return selected


def _collect(
    switches_config: dict[str, dict[str, str]],
    username: str,
    passwords: dict[str, str],
) -> dict[str, Any]:
    snapshots = {}
    for name, switch_config in switches_config.items():
        snapshots[name] = collect_switch_baseline(
            name,
            switch_config,
            username=username,
            password=passwords[name],
        )
    return snapshots


def _read_live_switches(
    config: dict[str, Any], username: str, passwords: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    live = {}
    capabilities = {}
    for name, switch_config in config["switches"].items():
        _LOGGER.info(
            "Switch %s: reading live VLANs and PVIDs from %s",
            name,
            switch_config.get("host", "UNKNOWN"),
        )
        live[name] = read_switch_state(
            name, switch_config, username, passwords[name]
        )
        capabilities[name] = switch_capabilities(
            name, switch_config, username, passwords[name]
        )
        _LOGGER.info(
            "Switch %s: model=%s firmware=%s; read %d VLANs; "
            "available SDK operations: %s",
            name,
            live[name]["model"],
            live[name]["firmware"],
            len(live[name]["vlans"]),
            ", ".join(sorted(capabilities[name])) or "none",
        )
    return live, capabilities


def _baseline_summary(
    config: dict[str, Any], inventory: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    vlan_names: dict[int, str] = {}
    for switch_name, switch in inventory.items():
        all_ids = set(switch["vlans"])
        all_ids.update(port["pvid"] for port in switch["ports"].values())
        for vid in all_ids:
            live_vlan = switch["vlans"].get(vid)
            name = live_vlan["name"] if live_vlan else f"VLAN{vid}"
            previous = vlan_names.get(vid)
            if previous is not None and previous != name:
                raise RuntimeError(
                    f"VLAN {vid} has conflicting names across switches: "
                    f"{previous!r} and {name!r} (including {switch_name!r})"
                )
            vlan_names[vid] = name

    configured_aliases: dict[int, str] = {}
    configured_vlans = config.get("vlans")
    if isinstance(configured_vlans, dict):
        for alias, definition in configured_vlans.items():
            if (
                isinstance(alias, str)
                and isinstance(definition, dict)
                and isinstance(definition.get("id"), int)
                and not isinstance(definition.get("id"), bool)
            ):
                configured_aliases.setdefault(definition["id"], alias)

    aliases: dict[int, str] = {}
    used_aliases: set[str] = set()
    for vid, name in sorted(vlan_names.items()):
        alias = configured_aliases.get(vid)
        if alias is None:
            alias = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower()
            if not alias or not alias[0].isalpha():
                alias = f"vlan_{alias}" if alias else f"vlan_{vid}"
        if alias in used_aliases:
            alias = f"{alias}_{vid}"
        used_aliases.add(alias)
        aliases[vid] = alias

    switches = {}
    for name, switch in inventory.items():
        vlans = switch["vlans"]
        ports = {}
        for number, port in switch["ports"].items():
            tagged = sorted(
                vid for vid, vlan in vlans.items()
                if number in vlan["tagged_ports"]
            )
            untagged = sorted(
                vid for vid, vlan in vlans.items()
                if number in vlan["untagged_ports"]
            )
            kind = "trunk" if tagged else "access"
            ports[number] = {
                "kind": kind,
                **(
                    {"vlans": [aliases[vid] for vid in sorted(set(tagged + untagged))]}
                    if kind == "trunk"
                    else {}
                ),
                "native_vlan": aliases[port["pvid"]],
            }
        switches[name] = {
            "host": config["switches"][name]["host"],
            "ports": ports,
        }
    definitions = {
        aliases[vid]: {"id": vid, "name": name}
        for vid, name in sorted(vlan_names.items())
    }
    return {"vlans": definitions, "switches": switches}


def main(argv: list[str] | None = None) -> int:
    parser = _HelpOnErrorParser(
        prog="vlanmgr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Command syntax:\n"
            "  baseline --config PATH [--switch NAME] [--output-dir DIR]\n"
            "  validate --config PATH\n"
            "  compile --config PATH\n"
            "  plan|diff --config PATH [--switch NAME]\n"
            "  apply --config PATH [--switch NAME] [--backup-dir DIR]\n\n"
            "Run 'vlanmgr COMMAND --help' for details on a command's options."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version('vlan-manager')}",
    )
    commands = parser.add_subparsers(
        dest="command", metavar="command", title="commands", required=True
    )
    baseline = commands.add_parser("baseline", help="collect a read-only switch snapshot")
    baseline.add_argument("--config", type=Path, required=True)
    baseline.add_argument("--switch", help="read only this configured switch")
    baseline.add_argument("--output-dir", type=Path, default=Path("snapshots"))
    validate = commands.add_parser("validate", help="validate desired-state YAML")
    validate.add_argument("--config", type=Path, required=True)
    compile_state = commands.add_parser(
        "compile", help="compile intents to vendor-neutral switch state"
    )
    compile_state.add_argument("--config", type=Path, required=True)
    for command_name, help_text in (
        ("plan", "show read-only switch changes"),
        ("diff", "alias for plan"),
        ("apply", "apply a confirmed reconciliation plan"),
    ):
        command = commands.add_parser(command_name, help=help_text)
        command.add_argument("--config", type=Path, required=True)
        if command_name in {"plan", "diff", "apply"}:
            command.add_argument(
                "--switch", help="plan or act on only this configured switch"
            )
        if command_name == "apply":
            command.add_argument("--backup-dir", type=Path, default=Path("backups"))
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command == "apply":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )

    try:
        config = _load_config(args.config)
        if args.command == "validate":
            validate_desired_state(config)
            print("Configuration is valid.")
            return 0
        if args.command == "compile":
            validate_desired_state(config)
            print(yaml.safe_dump(compile_desired_state(config), sort_keys=False))
            return 0

        if args.command in {"plan", "diff", "apply"}:
            validate_desired_state(config)
            all_desired = compile_desired_state(config)
            selected_config = _select_config_switches(config, args.switch)
            if args.command == "apply":
                _LOGGER.info(
                    "Validated configuration; planning apply for switches: %s",
                    ", ".join(selected_config["switches"]),
                )
            desired = {
                name: all_desired[name] for name in selected_config["switches"]
            }
            username = os.environ.get("TPLINK_USERNAME", "admin")
            passwords = _switch_passwords(selected_config)
            actual, capabilities = _read_live_switches(
                selected_config, username, passwords
            )
            plan = semantic_diff(desired, actual, capabilities)
            print(yaml.safe_dump(plan, sort_keys=False))
            if args.command in {"plan", "diff"}:
                return 0
            if any(item["unsupported_behavior"] for item in plan.values()):
                raise RuntimeError(
                    "Apply is blocked because the plan contains unsupported "
                    "device behavior"
                )
            if not has_changes(plan):
                print("All switches already match desired state.")
                return 0
            confirmation = input(
                "Type APPLY to write the displayed changes to the switches: "
            )
            if confirmation != "APPLY":
                print("Apply cancelled; no switch changes were made.")
                return 1

            _LOGGER.info("Explicit confirmation received; preparing backups")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_paths = {}
            for name, switch_config in selected_config["switches"].items():
                if not any((
                    plan[name]["vlans"]["create"],
                    plan[name]["vlans"]["update"],
                    plan[name]["vlans"]["delete"],
                    plan[name]["pvids"],
                )):
                    continue
                safe_name = _password_env_name(name).removeprefix(
                    "TPLINK_PASSWORD_"
                ).lower()
                backup_path = args.backup_dir / f"{safe_name}-{stamp}.bin"
                backup_paths[name] = backup_path
                if "backup_config" in capabilities[name]:
                    _LOGGER.info(
                        "Switch %s: retaining pre-apply backup at %s",
                        name,
                        backup_path,
                    )
                    backed_up = backup_switch_config(
                        name=name,
                        config=switch_config,
                        username=username,
                        password=passwords[name],
                        backup_path=backup_path,
                    )
                    if not backed_up:
                        raise RuntimeError(
                            f"Switch {name!r} advertised backup_config but did "
                            "not provide it"
                        )
                    _LOGGER.info("Switch %s: backup retained", name)
                else:
                    _LOGGER.warning(
                        "Switch %s: SDK does not support configuration backup",
                        name,
                    )

            for name, switch_config in selected_config["switches"].items():
                if name not in backup_paths:
                    continue
                _LOGGER.info("Switch %s: starting approved apply", name)
                apply_switch_plan(
                    name=name,
                    config=switch_config,
                    desired=desired[name],
                    approved_actual=actual[name],
                    username=username,
                    password=passwords[name],
                    backup_path=backup_paths[name],
                    backup_already_taken=(
                        "backup_config" in capabilities[name]
                    ),
                )
                _LOGGER.info("Switch %s: apply and read-back verification succeeded", name)
            print("Apply writes completed; current switch state verified by read-back.")
            return 0

        username = os.environ.get("TPLINK_USERNAME", "admin")
        selected_config = _select_config_switches(config, args.switch)
        switches_config = _baseline_switch_config(selected_config)
        baseline_config = {"switches": switches_config}
        passwords = _switch_passwords(baseline_config)

        switches = _collect(switches_config, username, passwords)
        snapshot = _baseline_summary(selected_config, switches)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        collected_at = datetime.now(timezone.utc)
        stamp = collected_at.strftime("%Y%m%dT%H%M%S.%fZ")
        output_path = args.output_dir / f"baseline-{stamp}.yaml"
        output_path.write_text(
            yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(output_path)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

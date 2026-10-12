# vlan-manager

Reconcile TP-Link Easy Smart switch VLAN membership and PVIDs from a
declarative YAML topology. The YAML describes desired state; it does not
configure router settings, Wi-Fi settings, or switch management addresses.

The project is alpha software. Review every plan before applying it to a real
network.

## Requirements

- Python 3.11 or newer
- Supported switch hardware: TL-SG108E and TL-SG1016DE
- Switch credentials provided through environment variables

## Install

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The editable install makes the `vlanmgr` command available in the active
environment. You can also invoke it as `python -m vlanmgr.cli`.

## Configuration

Start with [`docs/example.yaml`](docs/example.yaml). See
[`docs/configuration.md`](docs/configuration.md) for the meaning of VLAN
aliases, topology links, access/trunk/Wi-Fi ports, and the `parking` PVID.

Switch passwords do not belong in YAML. For switch `switch1`, set
`TPLINK_PASSWORD_SWITCH1`; for `switch2`, set `TPLINK_PASSWORD_SWITCH2`.
`TPLINK_PASSWORD` is available as a shared fallback. The SDK username defaults
to `admin`; override it with `TPLINK_USERNAME`.

## Commands

Validate configuration and inspect the vendor-neutral compiled intent without
connecting to switches:

```powershell
vlanmgr validate --config docs/example.yaml
vlanmgr compile --config docs/example.yaml
```

Read-only live operations:

```powershell
vlanmgr baseline --config docs/example.yaml
vlanmgr plan --config docs/example.yaml
vlanmgr diff --config docs/example.yaml
```

`baseline` writes a timestamped YAML inventory under `snapshots/` by default.
`plan` and `diff` read switch state and show the changes needed; neither writes
to devices.

To apply changes to one switch, first inspect its plan, then run:

```powershell
vlanmgr apply --config docs/example.yaml --switch switch1
```

`apply` displays the plan and waits for the exact confirmation `APPLY` before
writing. It retains a pre-change backup where the SDK supports it, then reads
the switch back to verify the result. Backups go under `backups/` by default.
Use `--switch` to limit plan/apply to a configured switch and `--backup-dir` to
choose another backup location.

## Safety

- `validate`, `compile`, `baseline`, `plan`, and `diff` do not change switches.
- `apply` is the only command that writes to switches and requires explicit
  confirmation.
- Extra VLANs and unsupported behavior are surfaced in the plan; unsupported
  plans fail closed.
- Keep production configs, credentials, snapshots, and backups private. The
  example addresses and names are illustrative; do not publish private network
  details from a live configuration.

## Tests

Run the unit suite from the repository root:

```powershell
python -m pytest
```

The tests use fake switch adapters and do not require access to real switches.

## License

This project is licensed under the GNU General Public License v3.0 only
(GPL-3.0-only); see [`LICENSE`](LICENSE). The TP-Link SDK dependency is a
separate work. Its upstream repository declares GPLv3; verify the license of
the exact SDK distribution you use before redistributing it.
The read-only `baseline` command only reads each switch name and management
`host` from the YAML; it identifies the supported model from the device. Its
timestamped output contains each switch's configured address, observed VLAN
IDs and names in a top-level `vlans` definition, followed by switch port
definitions that reference VLAN aliases by name. Each port includes inferred
`kind` and the `native_vlan` alias (the device PVID); trunks also include a
`vlans` list of carried aliases. Access ports omit `vlans` because their
untagged VLAN is already identified by `native_vlan`. A port with tagged VLAN
membership is reported as a trunk; otherwise it is reported as an access port.
This inference cannot distinguish a physical
inter-switch link or Wi-Fi access point from another port with the same live
VLAN memberships. For VLANs not defined in the input config, the baseline
generates aliases from the observed name and falls back to `VLAN<id>` if the
device does not report a name.
Use `--switch NAME` to collect only one configured switch.

The read-only `plan` command (also available as `diff`) validates the desired
configuration, reads each switch, and shows the VLANs to create, update, or
delete; per-port tagged/untagged membership changes; PVID changes; and device
limitations. Extra VLAN deletions are explicitly listed. Neither command
writes to switches. Use `--switch NAME` to limit device reads and the displayed
plan to one configured switch; intent compilation still uses the full topology.

`apply` displays the complete plan and requires typing `APPLY` before any
device write. Before the first write to each changed switch, it retains a
configuration backup when the SDK supports one. It then performs VLAN and PVID
changes by first removing untagged memberships not in desired state, then
applying destination VLAN memberships, and finally setting PVIDs. After PVID
changes, `apply` reapplies VLAN 1's desired membership because some switch
firmware restores stale VLAN 1 memberships during PVID changes. This also lets
`apply` repair existing ports reported as untagged members of multiple VLANs.
It then calls the SDK's `save_config()` method, reads the device state back,
and verifies it against the desired state. Backups default to `backups/` and
can be directed elsewhere with `--backup-dir`. If a write or verification
fails, the command reports the failing SDK operation and leaves the backup
available for recovery. Apply progress and SDK calls are logged to standard
error; the YAML plan remains on standard output. The reconciler never changes
the switch's 802.1Q enable setting. If VLAN mode is disabled, planned VLAN
changes are blocked rather than toggling that setting. Use `--switch NAME` to
limit reads and writes to one configured switch.

On a recognized login failure, the error identifies the switch-specific
`TPLINK_PASSWORD_<SWITCH_NAME>` variable to check; `TPLINK_PASSWORD` remains
available as the shared fallback. Password values are never included in logs
or error hints.

## Live Switch Integration Test

The write-capable real-device test is separate from the fake-adapter unit tests
and is skipped unless explicitly enabled. For ordinary CLI use, put the switch
management IP under `switches.<name>.host` in the YAML. Passwords belong in the
environment as `TPLINK_PASSWORD_<SWITCH_NAME>` (for example
`TPLINK_PASSWORD_TEST`) or the shared `TPLINK_PASSWORD` fallback; never put them
in YAML.

For the live test, set `VLANMGR_LIVE_TESTS=1`,
`VLANMGR_LIVE_HOST=<IP>`, and `VLANMGR_LIVE_SWITCH=TEST`, plus the
appropriate password environment variable, then run
`pytest -q tests/integration`. The test currently requires a TL-SG108E and
drives changes through the `vlanmgr compile` and `vlanmgr apply` CLI commands;
it checks the compiled state before any write, allowing only additional
non-parking tagged VLAN memberships on upstream port 1 as required by topology
propagation. Existing VLANs tagged only on port 1 are represented with the
router-facing link's explicit `tagged_vlans` intent so they can be preserved.
It tests ports 2-4 and uses port 1 as the upstream. The CLI retains
backups in a private per-run directory under `backups/live-tests/`. Test VLANs
are propagated as tagged memberships over port 1, which must retain VLAN 1
untagged and PVID 1. Afterward the test restores the original VLAN
memberships and PVIDs through the CLI, then removes the directory after
successful cleanup. If cleanup fails, it keeps the backup files for recovery.
States that cannot be represented exactly in CLI YAML are rejected before any
test writes; diagnostics log port PVIDs and VLAN memberships for observed,
requested, and compiled states when preflight comparisons fail. For a dedicated disposable test switch only,
`VLANMGR_LIVE_SKIP_RESTORE=1` bypasses the initial-state representability check
and skips cleanup restoration. Device changes may remain; per-run recovery
files are retained under `backups/live-tests/` and must be managed manually.

`VLANMGR_LIVE_TESTS=1 VLANMGR_LIVE_HOST=192.168.0.16 VLANMGR_LIVE_SWITCH=TEST PYTHONPATH=src /tmp/home-vlan-manager-venv/bin/python -m pytest -q tests/integration > live-test-output.txt 2>&1`

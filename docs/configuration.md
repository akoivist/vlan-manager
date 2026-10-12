# Configuration Intent

The YAML describes the intended topology and VLAN membership, not router or
Wi-Fi settings. VLAN keys (`management`, `home`, `iot`, `parking`) are aliases;
port and Wi-Fi declarations refer to aliases rather than raw VLAN IDs.

## Top-Level Values

- `router.name` names the topology root. It does not configure the router.
- `switches` names each switch and gives its `host`, `model`, `port_count`, and
  explicit port declarations. `host` is its management address.
- `wifi_networks` maps each Wi-Fi network to a VLAN alias. These VLANs are
  carried by Wi-Fi ports; this file does not configure SSIDs.
- `parking` declares the fallback PVID ID for undeclared ports and trunks
  without a native VLAN. It is not created as a switch VLAN; switches that use
  it receive only the PVID setting. It must not be referenced by a port or
  Wi-Fi assignment.

## Ports

The key under `ports` is a physical port number. `name` is only a descriptive
label. Ports may be undeclared: they carry no VLAN memberships and use
`parking` as PVID.
`port_count` states the switch's physical port count.

- `kind: link` connects the router or another switch. `upstream` names a peer
  closer to the router; `downstream` names a child switch. Declare the matching
  peer at the other end: `switch1` port 8 points downstream to `switch2`, whose
  port 8 points upstream to `switch1`. Both ends use `native_vlan: management`.
  VLAN 1 (`management`) is always untagged and is the PVID on every link.
  Other required VLANs are carried tagged for downstream needs; `parking` is
  never propagated over links. Any number of switches may have router-facing
  links, such as `switch1` port 1 pointing upstream to `router`. A
  router-facing link may also declare `tagged_vlans` for VLANs that must be
  carried to the router despite having no local port or Wi-Fi assignment; this
  is an explicit switch-port membership, not router configuration.
- `kind: access` connects one client. Its `native_vlan` is the single untagged
  VLAN and PVID. For example, `living-room-tv` uses `iot`.
- `kind: trunk` carries the VLANs listed in `vlans`. `native_vlan`, if set, is
  untagged and is the PVID; the other carried VLANs are tagged. Without a native
  VLAN, all listed VLANs are tagged and `parking` is untagged and used as the
  PVID. In the example,
  `server1` carries `management`, `home`, and `iot`, with `management` native.
- `kind: wifi` connects an access point. Its `native_vlan` is untagged; VLANs
  referenced by `wifi_networks` are tagged. `living-room-ap` uses `management`
  natively and carries `home` and `iot` tagged.

## Constraints

VLAN references must name declared aliases. A port can have at most one
untagged VLAN, which is also its PVID. On TL-SG1016DE, firmware reports VLAN 1
as untagged on every port even when another VLAN is active; the live-state
adapter treats VLAN 1 as effective only on ports whose PVID is 1. `parking` is
reserved for fallback use and must not be selected as a port or Wi-Fi VLAN.
Native-less trunks receive `parking` as an untagged membership and PVID; it
stays local and is never carried over switch or router links. Undeclared ports
use its ID as PVID without a membership. Every link uses VLAN 1 (`management`)
as its untagged VLAN and PVID. The switch topology may have multiple
router-connected roots but must otherwise be unambiguous, loop-free, and
connected to `router`. VLAN 1 remains on link and Wi-Fi ports. Never store
credentials in this file; switch passwords come from environment variables.

`plan` and `diff` are read-only and show per-switch VLAN creations, updates,
deletions, port membership changes, PVID changes, and unsupported behavior.
`apply` prints the plan and requires the operator to type `APPLY` before
changing any device. It backs up each switch where supported, performs writes
through the TP-Link adapter, calls `save_config()`, and reads state back to
verify the current state. Backups are stored in a private file under `backups/`
by default; use `--backup-dir` to choose another location. TL-SG108E writes are
persisted automatically; no separate save operation is required on the switch.

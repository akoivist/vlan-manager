# Project rules

- This project reconciles switch 802.1Q VLANs and port PVIDs from a runtime provided YAML-file. Example YAML is in docs/example.yaml.
- The YAML file is the desired-state source of truth. It contains intents for VLANs, port memberships, and PVIDs. The project reads the actual state from switches and computes a plan to reconcile the two.
- Do not configure the switch management IPs, or unrelated switch features.
- Use a vendor-neutral internal model. Keep all SDK-specific calls in adapters/tplink.py.
- Start with a read-only implementation. No command may write to switches unless the user explicitly requests apply.
- `plan` and `diff` must never make device changes.
- `apply` must show the complete plan and require an explicit confirmation before writing.
- Display extra VLANs that will be deleted before applying. Never silently delete them.
- Read and retain a per-switch backup before the first write in an apply run when the SDK supports it.
- Do not store passwords in YAML, source code, logs, or committed files.
- Use environment variables for switch admin credentials.
- Fail closed on unsupported switch models, incomplete port declarations, topology loops,
  ambiguous paths, missing VLANs, mismatched native VLANs, or failed device reads.
- Report hardware/firmware limitations and never claim a successful exact reconciliation if the device retains unwanted state.
- After writes, read the switch back and compare actual state to desired state.
- Use unit tests with a fake switch adapter. Tests must not require real switches.
- Add tests for VLAN creation/deletion, tagged and untagged memberships, PVIDs,
  trunk propagation, AP Wi-Fi VLAN propagation, invalid topology, and partial failures.
- VLAN 1 must always be present on all link and wifi ports. Do not allow its removal.
- Switch model TL-SG108E is fully supported by the tplink-tool-sdk and does automatic saving of changes.
- Only one untagged vlan is allowed per port. If a port has multiple untagged VLANs, the plan must fail closed.
- TPLINK switches do not support creating a VLAN without at least one tagged or untagged port membership. The plan must fail closed if a VLAN has no memberships.
- TPLINK switches do not support setting PVID value to a VLAN that does not exist. The plan must fail closed if a port's PVID is set to a VLAN that is not declared.
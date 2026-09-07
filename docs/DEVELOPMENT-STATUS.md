# Development status

This branch preserves additional development source alongside the default-branch corrections. It is not a release candidate.

The source includes changes made after the published 2.0.1 build. Existing release downloads have not been replaced.

Recent corrections make Dashboard, Firmware and Terminal device selectors keyboard accessible, retain focus during inventory refresh, and keep long labels inside their panels. Destination admission now uses one captured path identity for install coordination. Managed Mesh transport retains captured input during provider retirement. Tests were updated to exercise the current device, lifecycle and update-availability contracts. The README describes support and remaining limits more precisely.

Focused source and inert browser/Flask tests cover these changes. They do not establish whole-application, native-package or physical-device coverage. The Selected Device details panel can still retain initial data after selection changes; its separate display correction is pending.

Manual update availability checks are integrated. Complete download, installation, restart and rollback remain separate work. The durable BLE journal and managed Mesh provider are foundations; application persistence, Mesh chat/configuration and hardware validation remain incomplete. Firmware payload coverage, maps and terminal improvements remain in progress.

Additional source includes simulated transcript replay, firmware artifact validation and ESP image parsing. These do not establish physical behavior, complete offline payloads or full flashing integration. The older captured-input retirement defect is corrected in this source; Mesh interface adoption still remains.

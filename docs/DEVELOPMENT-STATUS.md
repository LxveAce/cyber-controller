# Development status

This branch checkpoints unfinished work on top of the default branch. It is not a release candidate. Published 2.0.1 downloads are unchanged.

- Manual update checks use an owned backend operation and a browser client that polls that operation. Independent frontend and backend reviews are complete, and four browser-to-Flask fixture cases pass; this update-check path is also on the default branch. Download, installation, restart and rollback are separate work.
- Managed Mesh serial transport is a source-only provider. It is not yet connected to the current Mesh interface. Review found a disconnect race where an already-read byte can be omitted from loss accounting; correction is pending.
- Firmware artifact validation and the ESP image parser provide host-side validation foundations. Enforcement against fresh board-build evidence remains a separate held candidate.
- Transcript replay provides simulated, unauthenticated fixture input for repeatable testing. It does not establish device identity or physical radio behavior.
- The durable BLE journal core is included; application ingestion, persistent storage selection and exports still need integration.

Hardware, full desktop packages, Linux distribution startup, and complete user flows remain under validation. Do not interpret fixture counts as whole-application coverage.

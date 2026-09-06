# Development status

The default branch contains changes made after the published 2.0.1 build. Existing release downloads have not been replaced.

Recent source changes improve device and callback cleanup, BLE report parsing and refresh, target selection and menu behavior, tool-download jobs, settings updates, and device identity snapshots. Manual update checks now use one owned background operation with bounded status polling and cleanup. The durable BLE journal and managed Mesh configuration core remain foundations awaiting application adoption.

Additional work is on the development/current branch. Complete update installation and rollback, Mesh chat and configuration in the current interface, firmware availability, and platform/device validation remain in progress. Source and fixture tests do not replace testing installed builds on supported hardware.

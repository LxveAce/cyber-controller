# Security review

Review changes against their failure cases, not just a successful run. Record the source revision, reproduction steps, impact and checks performed. Verify findings independently before treating them as confirmed. A passing test suite does not establish that every device or packaged build works.

Check these areas when changing a web route, device command or download path:

- Authentication, session handling and authorization for the selected device.
- CSRF protection on state-changing requests and socket events.
- Input types, length limits, control characters and command boundaries.
- Destination restrictions, redirects, response sizes and timeouts for network requests.
- Archive paths, checksums, rollback and selection of the installed version.
- Escaping of device names, scan results and other untrusted text in pages and exports.
- Credential storage, failure recovery and concurrent readers during password changes.
- Error responses, logs and accidental disclosure of sensitive data.

Use isolated fixtures for malformed replies and failure injection. Record actual hardware checks separately, including the board, firmware and operation tested. Confirm important interface behavior in the current app; a backend test cannot tell whether a panel is visible.

These are review requirements, not a blanket claim that every path has passed them. See [SECURITY.md](../SECURITY.md) for the security policy and [website checks](WEBSITE-SECURITY.md) for static pages.

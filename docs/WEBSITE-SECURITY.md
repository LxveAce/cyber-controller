# Website security checks

Static pages can still expose secrets, render unsafe remote data or load compromised dependencies. Review the source and the deployed response separately: a tag in an HTML file does not prove that the server sends a security header.

- Render release names and other remote text safely. Validate download links before placing them in the page.
- Keep credentials out of browser code, generated files and source maps. Public release lists should not need a personal access token.
- Restrict script and connection sources to what the page uses. Pin dependencies and verify externally hosted assets where supported.
- Check the deployed Content-Security-Policy, content-type handling, framing restrictions and HTTPS configuration. Document hosting limitations instead of claiming headers are active from source inspection alone.
- Check publishing workflows and generated output for backup files, private notes and other unintended content.
- Repeat these checks after changes to release rendering, third-party scripts, hosting or deployment.

Record the URL, deployment revision, date and evidence for each live check. Earlier source reviews do not establish the current deployed state. See [the application security review checklist](RED-TEAM.md).

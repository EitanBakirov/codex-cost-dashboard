# Security and privacy

Codex Cost Dashboard is designed to run entirely on the user's computer.

- It binds its HTTP server to `127.0.0.1` only.
- It does not send telemetry, session contents, account details, or usage data anywhere.
- It reads Codex session files and never modifies them.
- It writes only its selected-task preference under `$CODEX_HOME/cost-dashboard/`.

The dashboard API includes prompt text so the local browser can render the prompt inspector. Other processes running as the same user may be able to access localhost services, and browser extensions may be able to inspect pages. Avoid running the server on shared or untrusted computers.

Do not attach real Codex session logs to public bug reports. Reproduce parser issues with minimal, anonymized JSONL records instead.

To report a vulnerability, open a GitHub security advisory rather than a public issue.

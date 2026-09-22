# Security and privacy

Codex Cost Dashboard is designed to run entirely on the user's computer.

- It binds its HTTP server to `127.0.0.1` only.
- By default it does not send telemetry, session contents, account details, usage data, or any requests anywhere.
- It reads Codex session files and never modifies them.
- It writes only its selected-task preference under `$CODEX_HOME/cost-dashboard/`.

## Optional official-document update checks

The **Check official updates** button, `--check-openai-updates`, and the
`--check-openai-updates-daily` opt-in are the only features that contact the
network. They fetch two fixed, public OpenAI documentation URLs over HTTPS:

- `https://learn.chatgpt.com/docs/pricing.md`
- `https://learn.chatgpt.com/docs/models.md`

The request contains only a generic dashboard user agent. It does not include
prompt text, session paths, token counts, account identity, authentication, or
usage data. The local state file stores page hashes and response metadata so it
can report a change. A detected change never downloads or activates pricing;
the installed, versioned rate card remains authoritative until a reviewed
release updates it.

The dashboard API includes prompt text so the local browser can render the prompt inspector. Other processes running as the same user may be able to access localhost services, and browser extensions may be able to inspect pages. Avoid running the server on shared or untrusted computers.

Do not attach real Codex session logs to public bug reports. Reproduce parser issues with minimal, anonymized JSONL records instead.

To report a vulnerability, open a GitHub security advisory rather than a public issue.

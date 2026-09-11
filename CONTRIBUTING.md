# Contributing

Contributions are welcome, especially anonymized parser fixtures from new Codex log formats and verification on Windows or Linux.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`.

Keep the runtime dependency-free unless a dependency has a clear security and maintenance benefit. Never commit real session logs, authentication files, account identifiers, prompt text, or absolute home-directory paths.

The local Codex JSONL format is not a documented public API. Parser changes should be defensive, retain compatibility with older shapes, and include sanitized tests.

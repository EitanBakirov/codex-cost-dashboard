# Codex Cost Dashboard

A private, local-only dashboard for understanding Codex activity, token usage, and estimated cost across tasks.

It reads the Codex session logs already stored on your computer, serves the dashboard on `127.0.0.1`, and does not upload anything. The repository is the only online component.

> [!IMPORTANT]
> This is an independent community tool, not an official OpenAI product. Codex's local JSONL session format is not a documented public API and may change. Dollar values are estimates, not invoices, and the observed plan meter is not a token-to-plan conversion.

## What it shows

- A global view for the last 24 hours, since 7 AM, seven days, or all local history
- Estimated credit use, a configurable dollar equivalent, fresh input, cached input, and output tokens
- Current locally observed plan allowance and reset time
- Model usage mix
- A separate Models & pricing guide with the bundled credit rate table, model roles, reasoning-effort explanations, and an informal Sol-versus-Terra comparison from local sample data
- Task/session totals and prompt-by-prompt history
- Model, effort, duration, calls, tools, token counts, and estimated cost per prompt
- Compact skill, image, local-file, and clickable-link markers
- The currently signed-in Codex account and plan, decoded locally from the local auth file

## Requirements

- Codex with locally persisted session history
- macOS, Linux, or Windows
- Python 3.10 or newer (`python3 --version`)

Codex stores local state under `CODEX_HOME`, which defaults to `~/.codex`, according to the [official OpenAI Codex configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations). The dashboard follows that setting automatically.

## Install and run

### macOS or Linux

```bash
git clone https://github.com/EitanBakirov/codex-cost-dashboard.git
cd codex-cost-dashboard
# Use the Python 3.10+ executable on this computer (for example python3.12).
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .
codex-cost-dashboard --open
```

If `python3 --version` reports an older version (macOS can still ship Python
3.9), use the newer executable installed on your machine, such as `python3.12`
or `python3.11`.

If your shell still picks an older globally installed dashboard command after
activating the environment, use the installed module directly:

```bash
python -m codex_cost_dashboard.dashboard --open
```

### Windows PowerShell

```powershell
git clone https://github.com/EitanBakirov/codex-cost-dashboard.git
Set-Location codex-cost-dashboard
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\codex-cost-dashboard.exe --open
```

The page opens at [http://127.0.0.1:8766](http://127.0.0.1:8766). Keep the terminal open while using it and press `Ctrl-C` to stop.

You can also run it directly from the clone without installing anything:

```bash
python3.12 run_dashboard.py --open
```

PowerShell equivalent:

```powershell
py -3 run_dashboard.py --open
```

## Install with a coding agent

Give your local coding agent this prompt:

> Clone `https://github.com/EitanBakirov/codex-cost-dashboard.git`, install it in a project-local Python 3.10+ virtual environment using the instructions for this operating system, run its test suite, and launch `python -m codex_cost_dashboard.dashboard --open`. Keep it local-only, do not copy or upload my Codex session files, and do not change my Codex configuration.

## Options

```text
codex-cost-dashboard [--sessions-dir PATH] [--state-file PATH]
                     [--usd-per-credit NUMBER] [--port PORT] [--open]
                     [--check-openai-updates]
                     [--check-openai-updates-daily]
```

- `--sessions-dir`: override the default `$CODEX_HOME/sessions`
- `--state-file`: change where the selected-task preference is saved
- `--usd-per-credit`: customize the dollar-equivalent estimate. Credit purchase prices and discounts vary by plan, so this is not an invoice.
- `--port`: use another localhost port if `8766` is occupied
- `--open`: open the page in the default browser
- `--check-openai-updates`: explicitly fetch the two public official OpenAI documentation pages, report whether they changed, then exit
- `--check-openai-updates-daily`: opt in to at most one public documentation check per day when the dashboard starts. The Global / Plan page also has a **Check official updates** button for an explicit one-time check.

## Pricing and model maintenance

The bundled rate card is versioned with each dashboard release. It currently
uses the official ChatGPT **Standard-speed credit** rates, not API-key USD
prices. This distinction matters: ChatGPT credits are what Codex with ChatGPT
sign-in consumes, while API-key sessions use separate API pricing.

The **Models & pricing** guide is available from the far-right navigation tab.
Its table reads the same bundled rates as the calculator. It summarizes when
each general model fits, how fresh/cached/output token rates differ, and why
higher reasoning effort can change a task's duration and total usage without
changing its per-token rate. An anonymized, fixed sample of 36 Terra and 41
Sol turns across three tasks illustrates how extra model and tool steps can
outweigh a lower output-token rate. The sample is illustrative, not a live
analysis of each installer's logs or a model-quality benchmark. The guide is
bundled locally, so it does not make network requests when opened.

Celestial model families use small SVG icons before their names throughout the
dashboard: stars for Astra, sun for Sol, globe for Terra, and crescent for Luna.
Their colors are controlled by the four `--model-*` variables in
`src/codex_cost_dashboard/model_icons.css`; the SVG shapes remain unchanged when
the palette is adjusted. Numeric or unknown models keep their plain names.

The dashboard never automatically downloads or activates a new rate card. An
optional update check compares only these public documents:

- [Codex pricing and credit rates](https://learn.chatgpt.com/docs/pricing)
- [Codex models and retirements](https://learn.chatgpt.com/docs/models)

When either document changes, the dashboard shows a notice. Estimates remain
pinned to the installed rate card until a reviewed dashboard release updates
it. This prevents a silently changed webpage from rewriting local estimates.

For a manual terminal check:

```bash
python -m codex_cost_dashboard.dashboard --check-openai-updates
```

The companion `codex-cost-monitor` command provides a live terminal view. Use `codex-cost-monitor --help` for its options.

## Privacy model

The dashboard reads:

- `$CODEX_HOME/sessions/**/*.jsonl`
- `$CODEX_HOME/session_index.jsonl`, when present, for task names
- `$CODEX_HOME/auth.json`, when present, only to display non-secret account identity and plan claims locally

It writes only:

- `$CODEX_HOME/cost-dashboard/dashboard-state.json`
- `$CODEX_HOME/cost-dashboard/openai-update-state.json` only after an explicit update check or when the daily opt-in is enabled

It does not modify Codex sessions or authentication. By default it makes no external requests: no analytics, telemetry, CDN, external fonts, API calls, or background update checks. The optional update check contacts only the two public official documentation URLs listed above and sends no local session, token, account, or usage data. See [SECURITY.md](SECURITY.md) for the detailed boundary.

## Accuracy and limitations

- Estimates use token counters written to local Codex session logs and the rate table bundled with this release. The current rate-card version and source are exposed in the local API.
- Newer response records are counted once by response ID; older logs fall back to legacy token snapshots. Repeated snapshots do not add another model call.
- Automatic work without a user prompt is included in task and Global totals and shown separately. Guardian/automated-review logs are reported separately by token and call count, but excluded from the dollar estimate because their billing treatment is unknown.
- Included allowance, Fast mode, discounts, taxes, invoice adjustments, and server-side accounting are not available locally.
- The plan percentage is the latest rate-limit snapshot observed in local logs for the active account. Historical session logs may belong to a different account.
- Old sessions generally cannot be assigned reliably to an account because their logs may not contain account identity.
- Side chats and internal/automated work are included only when Codex persists enough local information to identify them safely.
- A prompt can be shown as **model not recorded** when its saved local log has no reliable model association. Its tokens remain included, but an exact model-specific cost cannot be recovered from those local records alone and is excluded from the estimate.
- A named model without a bundled rate remains visible but contributes no estimated cost until a rate is added.
- Local logs can change across Codex releases. Please file a sanitized fixture when a new shape is not recognized.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

The project intentionally uses only the Python standard library at runtime.

## License

[MIT](LICENSE)

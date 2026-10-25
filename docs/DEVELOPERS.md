# Kestrel for developers

[Back to README](../README.md)

Everything here is for developers and anyone who prefers a terminal. The app is the supported way
to use Kestrel; each command here does what a button does.

What the three installers have in common: a PyInstaller one-folder build of the same code, the
same `kestrel` entry point (no arguments opens the window, any argument runs the CLI), no bundled
Chromium, Ollama, or model, and nothing written beside the executable. The CLI is
`%LOCALAPPDATA%\Programs\Kestrel\kestrel-cli.exe` on Windows,
`/Applications/Kestrel.app/Contents/MacOS/Kestrel` on macOS, and the AppImage itself on Linux. Local
AI needs [Ollama](https://ollama.com); the app's Setup page helps you install it.

PDF rendering uses Edge, Chrome, or Playwright's Chromium, whichever is present; if none is, run
`<kestrel> doctor` for the install command.

## From source

```bash
git clone https://github.com/distantsoil/Kestrelapp && cd Kestrelapp
uv sync --extra dev
uv run kestrel setup              # offers the Ollama install command for your platform
uv run kestrel serve --open       # opens http://127.0.0.1:8765 in your browser
uv run python -m kestrel.desktop  # or the desktop window, where a webview backend exists
```

Building the installers yourself: `packaging/build_windows.ps1`, `packaging/build_dmg.sh`,
`packaging/build_appimage.sh`, each documented at the top of the file.

## `kestrel setup` from a terminal

One command, seven steps, each printed with a check or a cross, the reason, and the exact command
it ran or wants you to run. Nothing runs without your confirmation unless you pass `--yes`.

1. **Hardware.** Detects your OS, GPU memory (NVIDIA) or unified memory (Apple silicon), and RAM,
   and recommends a profile: `cpu_8gb`, `gpu_12gb`, `gpu_16gb`, `gpu_24gb`, `mac_16gb`,
   `mac_32gb`, `mac_64gb`. Override with `--profile`.
2. **Ollama.** Found and running, or the install command for your platform with an offer to run it.
3. **Base models.** Pulls the profile's models that are missing (`ollama pull`), about 3 to 20 GB
   once depending on the profile.
4. **Derived models.** Builds `kestrel-tier1`, `kestrel-tier2`, `kestrel-tier3` and
   `kestrel-docsel` with each tier's context window and guardrails baked in. This is the step
   that matters most; see "Models and guardrails" below.
5. **Profile recorded** so `kestrel doctor` and Settings can show which one is active.
6. **Doctor.** The full preflight, including the needle test that proves each model's real context
   window and the probe that proves thinking is off and JSON schemas are honored.
7. **Config seeded.** Prints where `profile.yml` is and what to do next.

Re-run `kestrel setup` any time; it skips what is already done. `kestrel setup --dry-run` prints
every command without running anything. `kestrel doctor` runs step 6 alone.

The first time the app opens it shows a "Finish setup" banner that leads to the Setup page
(`/setup`), where the same steps have buttons: choose Local, Hosted or Both, pull and build the
models, verify, and link to the hosted key and the resume import. The same steps are available as
`kestrel setup` from a terminal.


## No GPU? Run everything hosted

A machine with no supported GPU (setup recommends this unless it finds an NVIDIA GPU with at
least 12 GB of VRAM or an Apple silicon Mac with at least 16 GB) can skip Ollama and the
model download entirely and run every tier on OpenRouter, OpenModel, Anthropic or OpenAI under
your own key. Setup
offers this on every machine, including when Ollama is missing (choose "Use a hosted provider
instead"), and the three commands are:

```
kestrel setup --hosted            # pick the provider, paste the key, choose a model per tier
kestrel setup --hosted --free     # the same, offering only OpenRouter models priced at zero
kestrel serve --open              # then use it exactly as before
```

What it does: stores the key in `.env`, verifies it, lists the models that can honor a JSON
schema with context and price per million tokens, picks the cheapest for Tier 1 and document
selection and a stronger one for Tier 2 (override with `--model`, `--model-tier1`,
`--model-tier2`, `--model-docsel`; `--provider openmodel`, `anthropic` or `openai` for the
others, where Anthropic defaults to Haiku for Tier 1 and Sonnet for Tier 2 and OpenAI to the
mini and full gpt-5 models when the account lists them), shows the same Hosted
mode disclosure as Settings > Hosted and requires you to type `yes`, then writes the same
settings the page writes and records the `hosted_only` profile. `kestrel doctor` then reports the
Ollama checks as skipped rather than failed. Two things to know: free OpenRouter models carry a
daily request cap and may train on your inputs unless data collection is set to deny (the
shipped default, which can leave no free model to route to); and embeddings never go to a
provider: the `hosted_only` profile runs them on this machine with a small model bundled inside
Kestrel (`all-MiniLM-L6-v2` on the CPU), so few-shot examples and the reranker work with no
Ollama installed.

## Models and guardrails

**Which models.** The profile picks the base models per tier. Default (16 GB GPU): Tier 1
`qwen3:4b` for fast keep/reject, Tier 2 `qwen3:14b` for the six-block evaluation, document
selection `qwen3:4b`, embeddings `nomic-embed-text`. Smaller profiles step Tier 2 down to
`qwen3:8b`; 24 GB and 64 GB Mac profiles step it up to `qwen3:30b-a3b`. Edit
`kestrel/resources/config/models.yml` (or the copy in your config folder) to use anything else that
Ollama serves; `kestrel bench` measures a candidate against the gold set before you commit to it.

**What stops a model going astray.** Five mechanisms, all on by default:

| Guardrail | Where | What it prevents |
|---|---|---|
| Context window per tier, verified by a needle test | derived Modelfile `num_ctx`; `kestrel doctor` | The silent truncation that makes every score quietly wrong |
| Baked sampling limits: `temperature 0.1`, `top_p 0.9`, `repeat_penalty 1.1`, `seed 7`, `num_predict` per tier | derived Modelfile | Rambling, repetition loops, runaway output; a request that sets its own temperature or max tokens still wins |
| Schema-constrained output, one repair, then quarantine | every model call | Prose where JSON was required; a hallucinated key never reaches the database |
| Uncertainty escalates | pipeline rules | A low-confidence reject becomes keep; quarantined items move up a tier, never out |
| Thinking off, verified | `reasoning_effort: "none"` plus a doctor probe | A reasoning model spending ten times the tokens, or leaking `<think>` into JSON |

Two things the guardrails do not do: they do not make a small model smart (use `kestrel bench`
and a bigger profile), and they do not act on your behalf. Kestrel never submits, sends, or
applies; it drafts and hands over.


## Picking a model with `kestrel bench`

Kestrel ships a benchmark harness so you compare models on a gold set, changing exactly one variable
at a time:

```
kestrel bench --models qwen2.5:14b,llama3.1:8b        # compare two models, prompt fixed
kestrel bench --models qwen2.5:14b --prompt-version evaluate/v2   # compare two prompts, model fixed
```

For each track it runs the real tier-2 evaluation against every model over the gold set and prints a
table: first-attempt JSON validity rate, mean tokens in and out, latency, items per minute at your
configured concurrency, exact-verdict agreement and mean absolute score error against the gold set,
and the projected wall-clock for a 500-item run. Any figure computed from fewer than five samples is
labeled insufficient rather than shown as if it were solid. Promote the winner to a tier from the
Bench page in the app.

The answer key is looked up in this order: your own `goldset.<track>.regenerated.jsonl` in the
evals folder, then `goldset.<track>.jsonl` there (the checkout's placeholders), then the same
placeholders shipped inside the app under `kestrel/resources/evals/`, so `kestrel-cli bench` also
works in the installed app. The placeholders are clearly marked and invented; once you have labeled
your own opportunities, regenerate the key from your real judgments with
`kestrel goldset regen --track employment`. The bench output names the key it used ("your labels"
or "the shipped examples").

Each tier is also configured separately in Settings by base URL, model name, and API key. Point all
three at local Ollama, or point tier 2 at a hosted inference service.


## Operational traps

These are verified against live Ollama, Playwright, and Windows behavior. Learn them once to avoid surprises:

### Context window truncation (the critical one)

Ollama's default context window is far below Kestrel's needs. Long job descriptions are silently cut in half with no error, and scores are quietly wrong. **Solution:** Build the derived models with `kestrel models build` and verify with `kestrel doctor`. This is not optional.

### Disabling thinking (if using reasoning models)

If your Tier 2 model has reasoning capabilities, you likely want to disable them (they consume 5x tokens for little benefit on deterministic scoring). The parameter is backend-specific:

- **Ollama:** `reasoning_effort: "none"` in the LLM client config
- **oMLX or vLLM:** `extra_body.chat_template_kwargs.enable_thinking = false`

Sending the wrong one is silently ignored; you find out via a massive token bill. Kestrel strips `<think>` spans defensively regardless.

### Structured output or failure

Every LLM call returns schema-validated JSON. One repair attempt, then the response is quarantined with the raw output preserved for investigation. Never silently defaulted to a fallback. If this feels noisy, it's by design: a wrong score that you catch is better than a silent error.

### Document selection never invents

The model selects from your existing text. If selection fails, it defaults to your full document, flagged for review. If you see a hallucinated variant key in the generated PDF, it's a validation bug, not a feature. Report it.

### Sample sizes

Any number derived from fewer than five observations (comp intelligence, conversion rates, analytics summaries) is labeled "insufficient" rather than presented as solid.

### Ghost jobs and liveness

The system learns which employers post perpetually open roles (ghosts) and which opportunities go live and die fast (liveness). These features start weak and improve as labels accumulate.

## Common commands

```bash
kestrel setup                  # guided first run on any platform (--dry-run, --profile, --yes)
kestrel serve --open           # run the web UI without the desktop shell (macOS, Linux)
kestrel provider test          # verify a hosted key and list schema-capable models (BYOK)
```

```bash
# Preflight: full health check
kestrel doctor

# Build the derived num_ctx models (critical)
kestrel models build

# Run ingestion: fetch and normalize new opportunities
kestrel scan --all
kestrel scan --track employment
kestrel scan --source "indeed"

# Run the pipeline: Tier 0 sweep, then drain Tier 1, then Tier 2
kestrel run

# Drain Tier 1 or Tier 2 alone (for debugging or recovery)
kestrel run --tier 1
kestrel run --tier 2

# Force a single opportunity through both tiers
kestrel evaluate 42

# Render a document (your resume) as a PDF
kestrel render 1
kestrel render 1 --opportunity 42      # P3 selection for opp 42
kestrel render 1 --redact              # Redact org names

# Seed example documents (resume + capability statement)
kestrel doc seed

# Import your resume
kestrel doc import resume.md --track employment --doc-kind resume --accept

# Propose and accept a capability statement import
kestrel doc import capability.md --track consulting --doc-kind capability --accept

# Recompute calibration proposal from labels
kestrel calibrate

# Train the reranker on labeled data
kestrel train

# Export pipeline to JSON or CSV
kestrel export --format json
kestrel export --track employment --out opps.csv

# Timestamped backup (DB + config, never .env secrets)
kestrel backup
kestrel backup --out /path/to/backups

# Headless mode (API + worker, no desktop window)
kestrel serve

# Drain pending tasks without a new Tier 0 sweep
kestrel worker
```

## Local verification

Run `uv sync --extra dev`, then `uv run --extra dev python scripts/check.py` before pushing.
The gate runs lint and the full pytest suite with pytest-xdist, using half the logical CPUs,
capped at 8 workers with a minimum of 1. `KESTREL_TEST_WORKERS` overrides that default;
`--workers N` takes precedence over the environment. Tests use isolated processes, per-test
temporary data folders, and OS-assigned loopback ports. `--workers 0` runs the same suite serially.
Use `--junitxml <path>` to save individual outcomes for a serial/parallel comparison.
Set `KESTREL_LIVE_BROWSER=1` to include the opt-in browser tests in either mode.
`--quick` still selects only the existing packaging and notices guards and is not a push gate.

SQLite connections use WAL with `synchronous=NORMAL`, including migration connections.
This keeps the database consistent after a crash while avoiding a disk sync for every commit.
A power loss can discard recent committed transactions; queued tasks resume from their last
durable state (D-UI-174).

## Architecture notes

- **Local-first:** Inference runs on your Ollama instance by default. A hosted Tier 3 backend is optional and opt-in per opportunity.
- **Resumable:** Every task is a queue row; kill the process at any moment and restart. No lost state.
- **Offline:** All static assets (HTMX, Alpine, hand-written stylesheet, tiktoken encoding) are vendored. No CDN required.
- **Single machine, single user:** No accounts, no sync, no horizontal scaling. SQLite is the right database.
- **Windows, macOS and Linux:** Driven by `pywebview` (Edge WebView2 on Windows, WebKit on macOS and Linux), tray via `pystray`, PDF rendering via Playwright.


## Learn more

- [`SPEC.md`](../SPEC.md): core architecture, data model, pipeline, scoring, and platform traps
- [`SPEC-INTERFACES.md`](../SPEC-INTERFACES.md): desktop shell, web UI, API, CLI, and MCP server
- [`SPEC-MODULES.md`](../SPEC-MODULES.md): documents, research, learning loop, and intelligence
- [`PROMPTS.md`](../PROMPTS.md): every LLM prompt and schema
- [`CLAUDE.md`](../CLAUDE.md): standing orders and hard rules (for developers)


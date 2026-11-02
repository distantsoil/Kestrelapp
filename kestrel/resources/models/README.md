# Derived num_ctx models

Each derived `kestrel-<tier>` model bakes in an explicit `PARAMETER num_ctx` because Ollama's
OpenAI-compatible `/v1` endpoint cannot set the context window per request; without this, long
descriptions are silently truncated and every score is quietly wrong (SPEC.md 8.2).

The Modelfiles here are **generated**: they are the default hardware profile (`gpu_16gb`, the
reference RTX 4080 Super) from `kestrel/resources/config/models.yml`, rendered by
`kestrel.llm.profiles.render_modelfile`. A test asserts they stay byte-identical to the renderer, so
change `models.yml`, regenerate, and never hand-edit a `FROM` line:

```
python -c "from pathlib import Path; from kestrel.llm import profiles as p; \
[(Path('kestrel/resources/models')/f'Modelfile.{t}').write_text(p.render_modelfile(t, 'gpu_16gb')) for t in p.DERIVED_TIERS]"
```

| Derived model | num_ctx | num_predict | Used by (SPEC.md 8.1) | Base (gpu_16gb, placeholder) |
|---|---|---|---|---|
| `kestrel-tier1`  | 4096  | 400  | tier1 **and** trigger | `qwen3:4b-instruct-2507-q4_K_M` |
| `kestrel-tier2`  | 16384 | 4500 | tier2                 | `qwen3:14b` |
| `kestrel-tier3`  | 20480 | 6500 | tier3                 | `qwen3:14b` |

Every Modelfile also bakes in the `guardrails` block from `models.yml` (`temperature 0.1`,
`top_p 0.9`, `repeat_penalty 1.1`, `seed 7`). A request that sets `temperature` or `max_tokens`
wins over these; they are the floor for any call that does not set them.

Document selection (docsel) has no derived model of its own: it calls `kestrel-tier2` (D-LLM-10),
whose `num_ctx` is larger than docsel's budget. Two derived models with different contexts would
load as two runners.

The **trigger** tier reuses `kestrel-tier1` (same base, same 4096 budget), so there is no separate
`kestrel-trigger` model; preflight still validates the trigger tier's budget against
`kestrel-tier1`. `embed` uses `nomic-embed-text` directly (no derived model).

## Building

- `kestrel setup` detects the machine, picks a profile (`cpu_8gb`, `gpu_12gb`, `gpu_16gb`,
  `gpu_24gb`, `mac_16gb`, `mac_32gb`, `mac_64gb`), pulls that profile's base models, renders its
  Modelfiles under `<data home>/config/models/`, runs `ollama create kestrel-<tier> -f ...` for
  each, and records the choice in `<data home>/config/hardware_profile.json`.
- `kestrel models build` runs `ollama create` for each shipped (gpu_16gb) Modelfile here.
- The `hosted_only` profile (`kestrel setup --hosted`, recommended with no GPU and under 16 GB of
  RAM) has no base models and no derived models: every tier is routed to a hosted provider (OpenRouter,
  OpenModel, Anthropic or OpenAI) under the operator's own key, so nothing here is pulled or built and `render_modelfile` refuses
  it with a clear error. `kestrel doctor` then reports the Ollama checks as skipped. `embed`
  never goes hosted: this profile runs it on the bundled in-process CPU model
  (`embed_backend: inprocess`, `all-MiniLM-L6-v2`, SPEC.md 8.1.1), so nothing needs Ollama.

Preflight (`kestrel doctor`) reads each model's effective `num_ctx` via the native `/api/show`,
runs a needle test because metadata can lie, and warns when the configured models will not fit the
detected VRAM or RAM, naming the profile to switch to.

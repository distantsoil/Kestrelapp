# Kestrel — References

> Every external source this specification depends on. The build agent should fetch these when
> it needs detail beyond what the spec records. Verification status is stated honestly: a
> "verified live" entry was hit with a real request on 2026-07-30 and its response shape is
> recorded in `SPEC.md` Appendix A.

---

## Prior art this design draws from

| Project | URL | License | Use | What we take | What we leave |
|---|---|---|---|---|---|
| **career-ops** | https://github.com/santifer/career-ops | MIT (verified 2026-10-13) | Ideas only | The six-block evaluation rubric, the A-F scoring dimensions, the Block G legitimacy check, the STAR+Reflection story bank concept, the HTML-plus-Playwright PDF pipeline, the portal scanner config format | The entire agent-CLI orchestration layer. It is a prompt pack driven by a coding CLI, not a service; that is the part we are replacing |
| **JobSpy** | https://github.com/speedyapply/JobSpy | MIT (verified 2026-10-13) | Bundled dependency; notice ships in THIRD-PARTY-NOTICES | The whole library, as the board ingestion layer | Nothing. Used as-is via `pip install python-jobspy` |
| **parametric-resume-builder** | https://github.com/ConstanzaSchibber/parametric-resume-builder | MIT (verified 2026-10-13) | Ideas only | The parametric concept: `resume(parameters) -> PDF` with slots holding pre-authored variants. This is what converts resume tailoring from unreliable free generation into constrained selection | The LaTeX toolchain. We render HTML to PDF via Playwright instead |

"Ideas only" was checked on 2026-10-13 by scanning both repositories for any 12-word run shared
with Kestrel: parametric-resume-builder had none; career-ops shared only boilerplate (a standard
browser User-Agent string, the HTML viewport meta line, and a stock interview question). Ideas
carry no license obligation; credit is a courtesy. Keep it that way: copying text or code from
either repository would bring MIT's notice requirement with it (decision log D-LIC-1).

Supporting reading in career-ops worth fetching if the rubric needs detail:
`ARCHITECTURE.md`, `DATA_CONTRACT.md`, `modes/deep.md`, `modes/contacto.md`,
`docs/RUNNING_ON_A_BUDGET.md`, `docs/AUTOMATION.md`.

---

## Job board and ATS APIs

| Source | Endpoint | Status |
|---|---|---|
| **Greenhouse** | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | **Verified live.** 200, 401 jobs against `anthropic`, 5.8MB |
| Greenhouse detail | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` | **Verified live.** 200 |
| **Lever** | `GET https://api.lever.co/v0/postings/{company}?mode=json` | **Verified live.** 200, bare array, 51 postings against `ro`. Unknown slug returns 404 |
| **Ashby** | `GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | **Verified live.** 200, 58 jobs against `Ashby` |
| **Workday** | `POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | **Verified live.** 200, 2000 postings against `nvidia.wd5` |
| **SmartRecruiters** | `GET https://api.smartrecruiters.com/v1/companies/{company}/postings` | **Verified live.** 200 against `Visa` |
| **Paylocity Recruiting** | `GET https://recruiting.paylocity.com/recruiting/jobs/All/{id}` (list: JSON in the page's `window.pageData`) and `GET .../recruiting/Jobs/Details/{JobId}` (detail: `ld+json` JobPosting) | **Verified live 2026-10-21.** 200 HTML, no auth, no script challenge, 11 postings on one public board, whole board in one response. `robots.txt` is a 404 (no rules). The documented-looking `.../recruiting/v2/api/feed/jobs/{id}` returns `jobs: []` and is not used. Kestrel sends at most 1 request/second per host; a 429 makes the run partial. No JSON API exists for the list, so this reads the public page's own embedded data |
| **Workable** | `POST https://apply.workable.com/api/v3/accounts/{slug}/jobs` body `{}` (list) and `GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}` (detail) | **Verified 2026-10-21 from the maintainer's network; the build environment was rate-limited by Cloudflare on this host.** 200, no auth, `{results, total}`, 6 postings on one public board. **Pagination UNVERIFIED** (total equalled the result count; a `nextPage` token is followed if present). `apply.workable.com/robots.txt` allows all (`Disallow:` empty) with `Content-Signal: search=yes, ai-input=yes, ai-train=no`. Kestrel sends at most 1 request/second per host; a 429 makes the run partial |
| **ADP Workforce Now** | `GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&ccId={ccId}&lang=en_US&locale=en_US&$top=20&$skip=1` (list) and `.../job-requisitions/{itemID}?...` (detail) | **Verified live 2026-10-21.** 200, JSON, no auth, 85 postings against one public career center. Pages cap at 20, `$skip` is 1-based, stop at `meta.totalNumber`. `robots.txt` is not published (redirects to the ADP sign-in page), so there is no disallow; Kestrel still sends at most 1 request/second per host. A 429 makes the run partial. Source for the public link format and `$top`/`$skip`: a third-party probe report plus live career-center links, not ADP documentation |
| **Rippling** | `GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` (list) and `.../jobs/{uuid}` (detail) | **Verified live 2026-10-21.** 200, no auth, bare array, 667 rows (381 postings) against one public board. No pagination (`page`/`pageSize` ignored). `ats.rippling.com/robots.txt` disallows only `/internal/`; `api.rippling.com/robots.txt` is 404. Rate: Kestrel sends at most 2 requests/second per host; one list request plus one detail request per posting. A 429 makes the run partial |
| JobSpy docs | https://github.com/speedyapply/JobSpy#readme | Parameter reference, country list, per-site constraints |

Exact field names, gotchas, and pagination behavior for each are in `SPEC.md` Appendix A. Read
that before writing an adapter; several of the field names are counter-intuitive (Lever's title
field is `text`, Greenhouse's `location` is an object, Workday is a POST).

---

## Signal sources for the consulting track

| Source | Endpoint | Status |
|---|---|---|
| **SEC EDGAR full-text search** | `GET https://efts.sec.gov/LATEST/search-index?q={query}&forms=8-K` | **Verified live.** 200, 8,436 hits for `"chief information officer"` in 8-K filings |
| SEC EDGAR access policy | https://www.sec.gov/os/webmaster-faq#developers | **A descriptive User-Agent with a contact address is mandatory.** 10 requests/second ceiling |
| SEC 8-K item reference | https://www.sec.gov/files/form8-k.pdf | Item 5.02 covers officer departures. Item 1.05 covers material cybersecurity incidents, mandatory since 2023 |
| **SAM.gov Opportunities API** | `https://api.sam.gov/opportunities/v2/search` | **Not verified.** Requires a free API key; unauthenticated returns 404 |
| SAM.gov API docs | https://open.gsa.gov/api/get-opportunities-public-api/ | Key registration and parameter reference |
| USAspending | `https://api.usaspending.gov/api/v2/` | **Verified live.** 200, no key required. Marginal for opportunity discovery; useful for organization context |

---

## Ollama

| Topic | URL | Why it matters |
|---|---|---|
| **OpenAI compatibility** | https://docs.ollama.com/api/openai-compatibility | **Read this before writing the LLM client.** Confirms `response_format`, `reasoning_effort`, and `max_tokens` are supported on `/v1/chat/completions`, and that `tool_choice`, `logprobs`, and `n` are not |
| **Structured outputs** | https://docs.ollama.com/capabilities/structured-outputs | Confirms structured outputs work through the OpenAI-compatible API via `response_format`. The bare `format` parameter belongs to the **native** `/api/chat` |
| **Thinking control** | https://docs.ollama.com/capabilities/thinking | `reasoning_effort: "none"` is Ollama's mechanism. `chat_template_kwargs` is a vLLM/oMLX parameter and is silently dropped here |
| **Context length** | https://docs.ollama.com/context-length | **The most important page in this list.** The OpenAI-compatible endpoint cannot set `num_ctx`. It must be baked into a derived model with a Modelfile, or long descriptions are silently truncated |
| Modelfile reference | https://docs.ollama.com/modelfile | `PARAMETER num_ctx` syntax |
| Native API — show | https://docs.ollama.com/api/show | The only way to read a model's effective parameters, used by preflight |
| Embeddings | https://docs.ollama.com/capabilities/embeddings | `nomic-embed-text` usage |
| FAQ | https://docs.ollama.com/faq | `OLLAMA_NUM_PARALLEL`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_LOADED_MODELS` |

---

## Hosted providers (bring your own key, SPEC §6.4)

Egress from the build container was blocked for both hosts on 2026-09-30, so these entries are
**documented, not verified live**. `kestrel provider test` is the live verification; run it on the
operator's machine and record the result in the private build log.

| Topic | URL | Why it matters |
|---|---|---|
| **OpenRouter quickstart** | https://openrouter.ai/docs/quickstart | Base `https://openrouter.ai/api/v1`, `Authorization: Bearer sk-or-v1-...`, OpenAI-compatible `/chat/completions` |
| OpenRouter models | https://openrouter.ai/docs/api/api-reference/models/get-models | `GET /models`: `id`, `context_length`, `pricing.prompt/completion` (USD per token, strings), `supported_parameters` (look for `structured_outputs` / `response_format`) |
| OpenRouter key | https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key | `GET /key`: `label`, `usage`, `limit`, `limit_remaining`, `is_free_tier`, free-model daily caps. The only endpoint that inspects the key without spending. Kestrel's rail reads `usage`, `limit`, `limit_remaining` from it (D-BYOK-12). `GET /credits` (account totals) is **not used**: whether it needs a management key is unconfirmed. Anthropic and OpenAI have no balance endpoint for an ordinary key |
| **OpenRouter structured outputs** | https://openrouter.ai/docs/guides/features/structured-outputs | `response_format: {type: json_schema, json_schema: {name, strict, schema}}`. Support varies by model AND provider; without `provider.require_parameters: true` a request can land on an endpoint that ignores the schema |
| OpenRouter provider routing | https://openrouter.ai/docs/guides/routing/provider-selection | `provider.require_parameters`, `data_collection: "deny"`, `zdr`, `allow_fallbacks`, `order`, `only`, `ignore` |
| OpenRouter ZDR | https://openrouter.ai/docs/guides/features/zdr | Per-request `provider.zdr: true` is OR'd with the account setting; a request can never turn account-level ZDR off |
| OpenRouter reasoning | https://openrouter.ai/docs/guides/best-practices/reasoning-tokens | `reasoning: {effort: none|minimal|low|medium|high|xhigh|max, exclude, max_tokens}`. `exclude` hides but still bills. Some models ignore `none` |
| OpenRouter limits and errors | https://openrouter.ai/docs/api_reference/limits | 401 bad key, 402 no credits or key limit, 429 rate limit (`X-RateLimit-*`, `Retry-After`), error JSON `{error: {code, message, metadata.provider_name}}`. `:free` models: 20 req/min, 50/day under $10 lifetime credit |
| Provider out-of-credit signals | https://platform.claude.com/docs/en/api/errors | **Anthropic verified 2026-10-21 from the live docs:** error body `{"type":"error","error":{"type":"billing_error","message":...},"request_id":...}` with HTTP **402** `billing_error` ("an issue with your billing or payment information"). The page does not mention a "credit balance" 400; Kestrel also matches that text on a 400 for the Anthropic provider only (unverified, harmless if absent). **OpenModel** (operator-supplied docs): HTTP 402 with `INSUFFICIENT_BALANCE` on `/v1/*`. **OpenAI**: error code `credit_balance_exhausted` (per the plan's provider notes; `insufficient_quota` on 429 is OpenAI's long-standing unfunded-account code and is treated the same). **OpenRouter**: HTTP 402 (see limits row). Classified in `kestrel/llm/credit.py` (D-LLM-8) |
| OpenRouter attribution | https://openrouter.ai/docs/app-attribution | `HTTP-Referer` and `X-Title` (or `X-OpenRouter-Title`) are optional; the title does nothing without the referer |
| **OpenRouter chat parameters (2026-10-15)** | https://openrouter.ai/docs/api_reference/overview, https://openrouter.ai/docs/api-reference/parameters, https://openrouter.ai/docs/features/structured-outputs | **Checked 2026-10-15 against OpenRouter's own generated SDK, not the web pages:** openrouter.ai was unreachable from the build container (DNS failure, then a proxy refusal), so the operator-named pages could not be fetched. The stand-in is the official `openrouter` Python SDK 1.3.59 (https://pypi.org/project/openrouter/, repository github.com/OpenRouterTeam/python-sdk), which Speakeasy generates from OpenRouter's OpenAPI spec. Its `ChatRequest` carries `repetition_penalty` (float, "1.0 means no penalty. Values above 1.0 penalize repeated tokens more strongly. Not all providers support this parameter"), `frequency_penalty` and `presence_penalty` (float, -2.0 to 2.0), `response_format` as a union of `text`, `json_object`, `json_schema` (`json_schema: {name (a-z, A-Z, 0-9, _ -, max 64), description, schema, strict}`), `grammar` and `python`, `reasoning: {effort: max|xhigh|high|medium|low|minimal|none, summary}` with a top-level `reasoning_effort` shorthand that must not disagree with it, and `provider.require_parameters` ("If this setting is omitted or set to false, then providers will receive only the parameters they support, and ignore the rest"). The model list's `supported_parameters` values include `frequency_penalty`, `presence_penalty`, `repetition_penalty`, `response_format`, `structured_outputs`, `reasoning`, `reasoning_effort`, `include_reasoning`. Exactly what the SDK says about turning reasoning off: the **request** schema's `reasoning` object has two fields only, `effort` (whose values include `none`) and `summary`; there is no `enabled` field in the request schema. `enabled` appears on the **model record** (`default_enabled`: "the default when the client does not set `reasoning.enabled`"), together with `mandatory`, marking models on which effort `none` is rejected. A second fetch of https://openrouter.ai/docs/api_reference/overview on 2026-10-15 failed again (DNS: ENOTFOUND openrouter.ai), so the documented request shape could not confirm `{enabled: false}`. Kestrel therefore sends `reasoning: {effort: "none"}` where the model's record says it takes reasoning (`reasoning` or `reasoning_effort` in `supported_parameters`) and `mandatory` is false, and falls back to `reasoning: {enabled: false}` (the switch the model record names) otherwise; either way a 400 quarantines with the provider's text rather than retrying. **Live check required on the operator's machine:** send both shapes to one reasoning model and one mandatory model and confirm which is accepted and that reasoning tokens drop to zero. Kestrel also: sends a penalty only when `require_parameters` is off, or when the model's `supported_parameters` lists both the penalty and `structured_outputs` (that list is the union over the model's endpoints, so one endpoint with both is not proven; D-LLM-2); leaves `maxItems`/`minItems` out of every hosted strict schema except OpenRouter's `openai/*` and `google/*` families (Anthropic models reject bounds above 1 with a 400); records a model assigned while its record cannot be fetched as json_mode `unknown`, sent as `json_object` with `require_parameters` off; maps `structured_outputs` to a strict `json_schema`, `response_format` alone to `json_object` with `require_parameters` off, neither to prompt-only (D-UI-48). D-LLM-5 (2026-10-15): a third fetch of https://openrouter.ai/docs/api_reference/overview failed the same way (ENOTFOUND openrouter.ai), so the SDK-verified shape stands: the hosted Tier 1 first attempt and every repair after a loop or a reasoning overrun send `reasoning: {effort: "none"}` (Tier 1 only to a model whose record lists reasoning and marks it not mandatory); the repair after a loop adds `frequency_penalty` (SDK: float, -2.0 to 2.0) only when the model lists it and not `repetition_penalty`, never relaxing `require_parameters`. **Live check required:** confirm on a free reasoning model (e.g. Nemotron) that `effort: "none"` drops reasoning tokens to zero and that a request with `frequency_penalty` plus a strict schema still routes. **Re-check the pages above live on the operator's machine** |
| **OpenModel Messages API** | https://docs.openmodel.ai/en/docs/api-reference/messages | **Verified from operator-supplied extracts, 2026-10-12.** `POST /v1/messages`, Anthropic shape: `model`, `messages`, `max_tokens` (required), `system`, `temperature`, `top_p`, `top_k`, `tools`, `tool_choice`, `stop_sequences`. Auth `Authorization: Bearer om-...` or `X-Api-Key`; `anthropic-version` defaults to 2023-06-01. Serves Anthropic, DeepSeek, DashScope, Xiaomi, Kimi, MiniMax, Zai (e.g. `claude-sonnet-4-6`, `deepseek-v4-flash`, `qwen3-max`). Errors `{type:"error", error:{type, message}}`; 402 insufficient balance, 403 quota, 429 rate limit. Tool use "may vary by provider" |
| **OpenModel Responses API** | https://docs.openmodel.ai/en/docs/api-reference/responses | **Verified from extracts.** `POST /v1/responses` for OpenAI and DashScope: `input` (string or turns), `instructions` (system), `max_output_tokens`, `text` (JSON-schema structured output; listed for OpenAI, not in the DashScope table), `store` (default true), `tools`/`tool_choice`. Chat completions removed. Errors OpenAI-shaped |
| OpenModel models list | https://docs.openmodel.ai/en/docs/api-reference/models/listModels | **Verified from extracts.** `GET /v1/models` (authenticated, no pagination): `id`, `owned_by`, `supported_apis: ["messages","responses"]`. No pricing fields, no key-info endpoint; the key is verified by listing models |
| OpenModel privacy | https://www.openmodel.ai/ | Prompts pass through to upstream providers, not stored, not trained on; request metadata (model, tokens, cost, IP, UA) kept 180 days. No ZDR or data-collection control exists, so the Settings page says so |
| **Anthropic Messages API** | https://platform.claude.com/docs/en/api/messages | **From vendor docs, not exercised live from this environment.** `POST /v1/messages`, headers `x-api-key` and `anthropic-version: 2023-06-01`; `model`, `messages`, `max_tokens` (required), `system`. Structured output: `output_config: {format: {type: json_schema, schema}}` (strict dialect: every property required, `additionalProperties: false`); the first content block is then text holding valid JSON. Current models (Claude 5 family) reject `temperature`/`top_p`/`top_k` and a forced `tool_choice` (`any`/`tool`), so Kestrel sends neither. `output_config.effort`: low, medium, high, xhigh, max. Thinking is on by default and omitted from the body. A safety decline is HTTP 200 with `stop_reason: "refusal"` and `stop_details.category`; Kestrel quarantines it with that reason. Errors `{type: "error", error: {type, message}}`; 401 bad key, 429 rate limit, 529 overloaded |
| Anthropic models list | https://platform.claude.com/docs/en/api/models-list | `GET /v1/models?limit=1000` (paginated, default 20): `id`, `display_name`, `max_input_tokens` (context), `max_tokens` (output cap), `capabilities.structured_outputs.supported`. No prices; the key is verified by listing models |
| **OpenAI chat completions** | https://platform.openai.com/docs/api-reference/chat | **From vendor docs, not exercised live.** `POST /v1/chat/completions`, `Authorization: Bearer sk-...`; `max_completion_tokens` replaces the deprecated `max_tokens` (required by reasoning models); `response_format: {type: json_schema, json_schema: {name, strict: true, schema}}` with the strict dialect; reasoning models (o1/o3/o4, gpt-5) reject a non-default `temperature` and take `reasoning_effort`. Usage carries `prompt_tokens`/`completion_tokens`, no cost |
| OpenAI models list | https://platform.openai.com/docs/api-reference/models/list | `GET /v1/models`: `id`, `created`, `owned_by` only, every family (embeddings, audio, image, moderation) in one list. Kestrel drops non-chat ids by marker and keeps the rest with capability unknown |
| OpenAI Responses (shape reference) | https://platform.openai.com/docs/api-reference/responses | `input` messages, `text.format: {type: json_schema, name, strict, schema}`, `max_output_tokens`; reply text in `output_text` or `output[].content[].text` where `type == output_text`; usage `input_tokens`/`output_tokens` |

---

## Hosting outside the desktop (evaluated 2026-09-30, see `docs/WEBAPP.md`)

| Topic | URL | Conclusion |
|---|---|---|
| Workers limits | https://developers.cloudflare.com/workers/platform/limits/ | Free: 100k req/day, 10 ms CPU per request, 128 MB, no filesystem, no background process. Rules out the worker, the scheduler, SQLite, Playwright and JobSpy |
| Python Workers | https://developers.cloudflare.com/workers/languages/python/ | Pyodide; FastAPI runs, but only pure-Python or Pyodide-bundled packages. No aiosqlite, no Playwright, no local Ollama |
| Pages status | https://developers.cloudflare.com/workers/static-assets/ | Pages is in maintenance mode; Cloudflare directs new projects to Workers with static assets |
| D1 limits | https://developers.cloudflare.com/d1/platform/limits/ | 5 GB, 5M rows read/day, 100k rows written/day on free |
| Cloudflare Access (Zero Trust free) | https://developers.cloudflare.com/cloudflare-one/ | Up to 50 users, one-time-PIN email login, no app code. **This is the password protection** |
| Cloudflare Tunnel | https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/ | `cloudflared` publishes a localhost service through a Cloudflare hostname with no open port. **The recommended path**: Kestrel stays on the GPU machine |
| Workers AI pricing | https://developers.cloudflare.com/workers-ai/platform/pricing/ | 10k neurons/day free. Not a substitute for the tuned local tiers, and would move inference off the machine |

---

## Agent memory

| Topic | URL | Notes |
|---|---|---|

---

## Ollama install, profiles and the structured-output caveat (2026-10-12)

ollama.com was unreachable from the build container; the GitHub docs were fetched directly.

| Topic | URL | Why it matters |
|---|---|---|
| Windows install | https://raw.githubusercontent.com/ollama/ollama/main/docs/windows.mdx | Per-user installer, no admin, adds `%LOCALAPPDATA%\Programs\Ollama` to PATH, tray app on 11434; `winget install Ollama.Ollama` is the one-liner `kestrel setup` prints (winget id UNVERIFIED live) |
| macOS install | https://raw.githubusercontent.com/ollama/ollama/main/docs/macos.mdx and https://formulae.brew.sh/cask/ollama-app | The .dmg is preferred; the Homebrew cask is now `ollama-app` (old `ollama` token retired); CLI linked into `/usr/local/bin` on first start |
| Linux install | https://raw.githubusercontent.com/ollama/ollama/main/docs/linux.mdx | `curl -fsSL https://ollama.com/install.sh \| sh`, systemd service, `systemctl edit ollama` for env vars |
| FAQ (env vars) | https://raw.githubusercontent.com/ollama/ollama/main/docs/faq.mdx | `OLLAMA_MODELS`, `OLLAMA_HOST`, `OLLAMA_NUM_PARALLEL` (default 1), `OLLAMA_KEEP_ALIVE` (default 5m), `OLLAMA_CONTEXT_LENGTH` (default 4096) |
| **Schema ignored with thinking off** | https://github.com/ollama/ollama/issues/14645 | Reported for the qwen3.5 family: `format`/`response_format` is ignored when thinking is disabled. Doctor's schema probe exists for this; a model that fails it is not fit for any tier |
| Modelfile parameters | https://docs.ollama.com/modelfile | `num_ctx`, `num_predict`, `temperature`, `top_p`, `top_k`, `repeat_penalty`, `seed`, `stop`; no `think` parameter; `/v1` cannot set `num_ctx`, `top_k` or `repeat_penalty`, so they must be baked in |
| Model tags | (library unreachable) | Profiles use the Qwen3 family (`qwen3:4b/8b/14b/30b-a3b`) and `nomic-embed-text`; newer families (`qwen3.5`, `qwen3.6`, `gemma4`, `ministral-3`) were seen only in third-party pages and are UNVERIFIED as library tags |

---

## Python stack

| Library | Docs | Notes |
|---|---|---|
| FastAPI | https://fastapi.tiangolo.com | |
| SQLAlchemy 2.0 async | https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html | Requires `aiosqlite`; Alembic runs a sync engine on the same file |
| Alembic | https://alembic.sqlalchemy.org | Running migrations programmatically matters for a frozen app |
| aiosqlite | https://aiosqlite.omnilib.dev | |
| SQLite WAL | https://www.sqlite.org/wal.html | `journal_mode=WAL` and `busy_timeout` are required for worker plus web coexistence |
| pydantic v2 | https://docs.pydantic.dev/latest/ | Field validators for truncate-not-reject on `maxLength` |
| pydantic-settings | https://docs.pydantic.dev/latest/concepts/pydantic_settings/ | |
| httpx | https://www.python-httpx.org | |
| Playwright Python | https://playwright.dev/python/docs/intro | |
| Playwright browser channels | https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge | `channel="msedge"` avoids bundling 150MB of Chromium |
| Playwright PDF | https://playwright.dev/python/docs/api/class-page#page-pdf | `header_template` and `footer_template` must be set empty |
| pywebview | https://pywebview.flowrl.com | Edge WebView2 on Windows |
| pystray | https://pystray.readthedocs.io | Tray icon and menu |
| PyInstaller | https://pyinstaller.org/en/stable/ | One-folder, not one-file |
| Inno Setup | https://jrsoftware.org/isinfo.php | Per-user install, no admin |
| APScheduler | https://apscheduler.readthedocs.io | `AsyncIOScheduler` for recurring scans |
| simpleeval | https://github.com/danthedeckie/simpleeval | Sandboxed rule expressions, never `eval()` |
| scikit-learn HGBR | https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html | The reranker. No torch |
| structlog | https://www.structlog.org | |
| typer | https://typer.tiangolo.com | |
| HTMX | https://htmx.org/docs/ | **Vendor it. No CDN at runtime** |
| Alpine.js | https://alpinejs.dev/start-here | Vendor it |
| Tailwind CLI | https://tailwindcss.com/docs/installation | Build the stylesheet once, commit it. The Play CDN needs network |
| markdownify | https://github.com/matthewwithanm/python-markdownify | |
| tiktoken | https://github.com/openai/tiktoken | Portable token counting approximation |
| fastmcp | https://gofastmcp.com | MCP server. Note the construction-time `allowed_hosts` behavior |
| win11toast | https://github.com/GitHub30/win11toast | |
| ntfy | https://docs.ntfy.sh | Optional phone push |
| uv | https://docs.astral.sh/uv/ | |
| respx | https://lundberg.github.io/respx/ | HTTP mocking for tests |

---

## Browser extension

| Topic | URL |
|---|---|
| Manifest V3 overview | https://developer.chrome.com/docs/extensions/develop/migrate/what-is-mv3 |
| Content scripts | https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts |
| Context menus | https://developer.chrome.com/docs/extensions/reference/api/contextMenus |
| Host permissions | https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions |
| Edge extension parity | https://learn.microsoft.com/en-us/microsoft-edge/extensions-chromium/ |

---

## Agent tooling

| Topic | URL | Notes |
|---|---|---|
| Warp Rules | https://docs.warp.dev/agent-platform/capabilities/rules/ | **`AGENTS.md` is the default filename.** `WARP.md` still works and wins if both exist. Must be ALL CAPS. Root plus subdirectory. Auto-applied |
| Warp Planning | https://docs.warp.dev/agent-platform/capabilities/planning/ | `/plan` builds a versioned plan; prompt phase by phase against it |
| Warp Task Lists | https://docs.warp.dev/agent-platform/capabilities/task-lists/ | Auto-created, executed in sequence |
| Warp permissions | https://docs.warp.dev/agent-platform/capabilities/agent-profiles-permissions/ | Four autonomy levels per action type. "Run until completion" is Ctrl+Shift+I on Windows and **ignores the denylist** |
| Warp orchestration | https://docs.warp.dev/platform/orchestration/ | `/orchestrate` spawns child agents, exactly one level deep, user approval required |
| Warp MCP | https://docs.warp.dev/agent-platform/capabilities/mcp/ | `~/.warp/.mcp.json` global, `.warp/.mcp.json` per project |
| Warp credits | https://docs.warp.dev/support-and-community/plans-and-billing/credits/ | Token-metered and non-deterministic. Build tier is 1,500/month. Opus burns fastest |
| **Warp known issue: rules dropped after summarization** | https://github.com/warpdotdev/warp/issues/7199 | **Read this.** Rules and sometimes the denylist stop being honored after the context window fills and compaction runs. This is why every phase prompt in `BUILD_PROMPT.md` restates its constraints inline |

---

## Update checker and release hosting

| Topic | URL | Notes |
|---|---|---|
| Cloudflare Pages limits | https://developers.cloudflare.com/pages/platform/limits/ | **Not verified live.** 25 MiB cap per static file. This is why installers stay on GitHub Releases and the site links to the asset URLs |
| Cloudflare Pages `_headers` | https://developers.cloudflare.com/pages/configuration/headers/ | **Not verified live.** Used on the website for `/latest.json` with `Cache-Control: max-age=300` (`docs/RELEASING.md`) |
| GitHub fine-grained personal access tokens | https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens | **Not verified live.** The `WEBSITE_TOKEN` secret: Contents read and write on `distantsoil/kestrelapp-site` only (D-UPD-5) |

---

## Standards and formats

| Topic | URL | Notes |
|---|---|---|
| JSON Schema | https://json-schema.org/understanding-json-schema/ | Constrained decoding supports only a subset. `patternProperties`, `oneOf`, `allOf`, and `if/then` are unsafe |
| MCP specification | https://modelcontextprotocol.io/docs | |
| robots.txt | https://www.rfc-editor.org/rfc/rfc9309.html | Respected on every fetch |
| WCAG 2.1 AA | https://www.w3.org/WAI/WCAG21/quickref/ | Contrast targets for the orange-on-white palette |

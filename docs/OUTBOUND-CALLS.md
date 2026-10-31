# Every call Kestrel makes

The source for the website's outbound-calls page (D-REL-3, owner 2026-10-30: "I do not want a
potential hidden call that gets listed as a problem by the public"). From a code audit on
2026-10-30. `tests/test_outbound_inventory.py` keeps it true: every web address in the shipped
code (`kestrel/`) must be in the host index at the end of this file with its kind, and every
host listed there must still be in the code.

Kestrel has no account, no analytics, no telemetry and no crash reporting. Its own pages load
nothing from other sites: fonts, htmx and Alpine ship with the app, and every page script talks
only to Kestrel's own server on this computer.

## Calls Kestrel makes

### Job sources (all off until the person turns one on or presses Start searching)

| Purpose | Destination | Sent |
|---|---|---|
| Indeed and Glassdoor searches (through the JobSpy library) | `apis.indeed.com`, `www.glassdoor.com` | Search words, place and filters from the person's profile |
| ZipRecruiter and Google Jobs (only if the person ticks them) | `www.ziprecruiter.com`, `www.google.com` | The same |
| Company job boards: Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling, ADP, Paylocity, Workable, Recruitee, Teamtailor, Personio, Breezy HR | Each system's public job board for that company | The board's name only |
| Find a board from a company name (on a click) | The board hosts above, up to 12 guesses | A name derived from what the person typed |
| Follow a Greenhouse short link found in a posting | `grnh.se`, one request, redirects off | Nothing but the request (at most 20 a run) |
| A careers page, job feed or portfolio page the person pastes | That site | Nothing but the request (private network addresses are refused) |
| USAJobs | `data.usajobs.gov` | Search words, place, the person's own key, and the email the person entered (USAJobs requires it) |
| Adzuna | `api.adzuna.com` | Search words, place, the person's own app ID and key |
| RemoteRocketship (the person's paid key) | `www.remoterocketship.com` | The key, target titles and on-site metro names; then the employer pages it returns |
| Himalayas | `himalayas.app` | Country (US) and a search word |
| We Work Remotely | `weworkremotely.com` | Nothing (a public feed) |
| SAM.gov and SEC EDGAR (consulting track) | `api.sam.gov`, `efts.sec.gov`, `www.sec.gov` | Codes or search phrases; SEC requires a contact email in the request |

Every request Kestrel sends itself carries Kestrel's own User-Agent and is rate-limited per site.
robots.txt, stated plainly (owner, 2026-10-30: "We just need to be transparent"; corrected from a
code audit the same day, which found more than the two exceptions first written here):

- **Checked before reading:** Workable, Recruitee, Teamtailor, Personio, Breezy HR, Himalayas, We
  Work Remotely, USAJobs, a careers page or job feed the person pastes, a role added by its
  address, the "is it still open" re-check of saved roles, a company's own site read for research,
  and portfolio pages. A disallow stops the read.
- **Not checked:** the public job-board APIs of Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  Rippling, ADP and Paylocity (each company's own public board, read through the address the
  system publishes for it); the keyed APIs of Adzuna (its robots.txt disallows crawlers; Kestrel
  makes the keyed search its API terms allow for a personal job search, D-SRC-16),
  RemoteRocketship (its robots.txt disallows `/api`; the keyed call is what its paid plan sells,
  D-SRC-14) and SAM.gov; SEC EDGAR (its own fair-access rules: a contact in the
  User-Agent and at most 10 requests a second); RSS or Atom feeds the person adds; and a
  Greenhouse short link (`grnh.se`), followed one hop to learn which board it points at.
- **Indeed and Glassdoor** go through the JobSpy library, which sends its own headers (it presents
  itself as a web browser, and as Indeed's phone app) and does not check robots.txt.

### AI providers (only after the person chooses an online model and approves what is sent)

| Purpose | Destination | Sent |
|---|---|---|
| Model calls | `openrouter.ai`, `api.openmodel.ai`, `api.anthropic.com`, `api.openai.com`, or a service address the person enters | What the approval screen lists for that step (role text, profile, and resume text only for Assess and Suggest) and the person's key |
| Key check and model list | The same | The key only, when it is saved or the picker opens |
| OpenRouter balance | `openrouter.ai` | The key only, about every 5 minutes, only in Hosted mode with OpenRouter |

Embeddings never go online; they run inside Kestrel.

### Updates

| Purpose | Destination | When and what |
|---|---|---|
| Check for a new version | `kestrelapp.co/latest.json` | About a minute after launch, then daily; on by default, switch in Settings. Sends no ID, version or personal data; never downloads anything |

### Other

| Purpose | Destination | When and what |
|---|---|---|
| Re-check that saved roles are still open | Each role's own posting page | Every 3 days for shortlisted and applied roles; nothing but the request |
| Company research | The company's own site (up to 6 public pages), SEC EDGAR | When a company page is opened with a local model, then weekly; on research clicks |
| Add a role found elsewhere | The pasted address | On the click, one page |
| Phone notifications | `ntfy.sh` or a webhook the person sets | Only if set in the config file (off by default, no screen for it yet); notification text, redacted in Confidential mode |
| Setup check | The person's enabled job boards | On the Setup "verify" button |

## What stays on the computer

- The local AI: Ollama at `127.0.0.1:11434` (the address can be changed; then it is wherever the
  person points it).
- Kestrel's own server on `127.0.0.1`, which the window, the browser extension and the page-timing
  record talk to.
- The MCP server and LAN access are off by default and accept connections; they never call out.

## Links that open only when clicked

Kestrel's site, the source repository and its changelog, the donation page, the projects Kestrel
builds on, the key sign-up pages (USAJobs, Adzuna, OpenRouter, OpenModel, Anthropic, OpenAI), the
providers' privacy pages, Ollama's download page, SAM.gov, the "Suggest a board" page, and each
role's posting and company site.

## Outside Kestrel's code

- The window engine: WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux. Kestrel loads only
  its own local address; the engine's own background traffic belongs to the operating system.
- PDF rendering drives the Edge or Chrome already installed, with the document set directly (no
  page is loaded from the web); the browser's own background traffic is outside Kestrel.
- Ollama downloads the models the person chooses from its own servers.
- JobSpy (Indeed, Glassdoor) sends its own request headers, not Kestrel's.

## Build time only (never from an installed app)

The installer build downloads the bundled embedding model (Hugging Face, hash-checked), packaging
tools, and a code-signing timestamp.

## Host index

Kinds: **call** (Kestrel connects to it), **link** (opens only when the person clicks), **local**
(stays on the computer or the person's own network), **reference** (text only: a comment, a
license, an example or an XML namespace; never contacted), **build** (used only when building the
installer). `{*}` stands for a part that comes from the person, a posting or a setting.

- `127.0.0.1`: local. Kestrel's own server.
- `127.0.0.1:11434`: local. Ollama, the local AI.
- `aka.ms`: reference. A license notice.
- `alpinejs.dev`: reference. Text inside the bundled Alpine library.
- `api.adzuna.com`: call. Adzuna search, the person's own key.
- `api.anthropic.com`: call. Anthropic models, after approval.
- `api.ashbyhq.com`: call. Ashby company boards.
- `api.example.com`: reference. A placeholder shown in Settings.
- `api.lever.co`: call. Lever company boards.
- `api.openai.com`: call. OpenAI models, after approval.
- `api.openmodel.ai`: call. OpenModel models, after approval.
- `api.rippling.com`: call. Rippling company boards.
- `api.sam.gov`: call. SAM.gov, consulting track, the person's own key.
- `api.smartrecruiters.com`: call. SmartRecruiters company boards.
- `apply.workable.com`: call. Workable company boards.
- `boards-api.greenhouse.io`: call. Greenhouse company boards.
- `careers.example.com`: reference. An example address.
- `console.openmodel.ai`: link. OpenModel key page.
- `data.usajobs.gov`: call. USAJobs search, the person's own key.
- `developer.adzuna.com`: link. Adzuna key sign-up.
- `developer.usajobs.gov`: link. USAJobs key sign-up.
- `docs.astral.sh`: reference. Help text in the setup check.
- `docs.openmodel.ai`: reference. A code comment.
- `docs.python.org`: reference. A license notice.
- `efts.sec.gov`: call. SEC EDGAR search, consulting track and company research.
- `en.wikipedia.org`: reference. The source note of the country list.
- `example-capital.com`: reference. An example address.
- `example.com`: reference. Example addresses and the default contact placeholder.
- `github.com`: link. The source repository, the projects Kestrel builds on, "Suggest a board".
- `himalayas.app`: call. Himalayas remote jobs.
- `huggingface.co`: build. The bundled embedding model, fetched when the installer is built.
- `kestrelapp.co`: call. The daily update check (and links to the download page).
- `ko-fi.com`: link. The donation page.
- `ntfy.sh`: call. Phone notifications, only if set in the config file.
- `ollama.com`: link. Ollama's download page (the command-line setup can also run Ollama's install script, after asking).
- `openrouter.ai`: call. OpenRouter models and balance, after approval (and its key page link).
- `platform.claude.com`: link. Anthropic key page.
- `platform.openai.com`: link. OpenAI key page.
- `recruiting.paylocity.com`: call. Paylocity company boards.
- `sam.gov`: link. SAM.gov key sign-up.
- `schemas.openxmlformats.org`: reference. An XML namespace in the Excel reader.
- `scripts.sil.org`: reference. The font license.
- `teamtailor.com`: reference. An XML namespace in the Teamtailor reader.
- `weworkremotely.com`: call. We Work Remotely's public feed.
- `workforcenow.adp.com`: call. ADP company boards.
- `www.anthropic.com`: link. Anthropic's privacy policy.
- `www.apple.com`: reference. A macOS settings-file format name.
- `www.linkedin.com`: reference. A code comment (LinkedIn is never searched).
- `www.openmodel.ai`: link. OpenModel's privacy policy.
- `www.remoterocketship.com`: call. RemoteRocketship, the person's paid key.
- `www.sec.gov`: call. SEC EDGAR filings.
- `www.w3.org`: reference. An SVG namespace.
- `{*}`: call. A site the person or a posting supplies: a pasted careers page or feed, a role's posting page, a company's own site, another AI service the person sets up, a Teamtailor career site, or a LAN address Kestrel shows for its own server.
- `{*}.breezy.hr`: call. Breezy HR company boards.
- `{*}.jobs.personio.com`: call. Personio company boards.
- `{*}.jobs.personio.de`: call. Personio company boards.
- `{*}.jobs.personio.{*}`: call. Personio company boards in another country domain.
- `{*}.recruitee.com`: call. Recruitee company boards.
- `{*}.{*}.myworkdayjobs.com`: call. Workday company boards.

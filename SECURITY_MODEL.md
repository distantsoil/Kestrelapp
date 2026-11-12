# Security model

This document says what Kestrel defends, against whom, and which code or decision enforces each
claim. If a claim has no enforcing code that I could find, it is listed under "Not verified" at
the end and is not claimed above it. Paths are relative to the repository root. "D-" IDs refer to
the private decision log. Some IDs are reused for different issues (D-SEC-8 and D-SEC-9 each appear twice), so
they are cited here with their issue number.

## 1. What Kestrel protects

- **Your job search data**: applications, profile and resume text, notes, contacts, ratings and
  generated documents, all in the data folder (`kestrel/paths.py`).
- **Your keys**: saved online model keys and the LAN shared key. Keys stay in `.env` and are not
  written into backups (CLAUDE.md, Privacy; D-SEC-11 amendment).
- **Your computer**: Kestrel does not run programs, write files outside its data folder, or act on
  the host for someone on the network (D-SEC-9 item 3; D-SEC-10 item 2; D-SEC-8, issue 150).

The public promises, and what the code does about each:

- **Never applies, emails or messages anyone.** CLAUDE.md hard rule 1 states it. I found no code
  path that submits a form, sends mail or clicks apply, but no test that proves the absence (see
  Not verified).
- **Asks before anything goes to an online model.** `kestrel/services/online_consent.py`
  `consent_problem` requires the confirmation, the same model as shown, Hosted mode on, and no
  employer hold. `tier_goes_online` makes automatic tasks hold rather than send (D-LLM-15).
- **Local by default.** Ollama runs on this computer. A local model request carries no tools, no
  files and no web access (D-PRIV-143; `kestrel/llm/guard.py`; `tests/test_local_ai_privacy.py`).
- **Free, and you are not the product.** A business promise. I found no code that enforces it.

## 2. Who the adversaries are

- **A web page in your browser** that tries to reach `localhost`. Defended by the cross-origin
  refusal in `kestrel/web/app.py` (`_reject_cross_origin`). See section 7.
- **Someone on your network without the key.** Every non-loopback request needs the LAN key
  (`kestrel/web/auth.py` `install_lan_auth`; `kestrel/mcp/server.py` `RequireLanKey`).
- **Someone on your network with the key.** A key holder can use the app but cannot change what
  leaves the computer or take the data (D-SEC-9, issue 155).
- **A visitor through a published tunnel.** The tunnel connects from 127.0.0.1, but the request
  carries a public Host name, so it is remote and needs the key (D-SEC-8, issue 151).
- **Hostile content**: job postings, company pages, model replies, imported files and restored
  backups (section 6).
- **A hostile or careless model server.** Its replies are format-checked, not trusted for truth
  (section 6; D-PRIV-143).

## 3. The three trust levels

**This computer.** A loopback peer, a Host of `localhost`, `127.0.0.1` or `::1`, and no proxy
header (`kestrel/web/auth.py` `peer_is_loopback`; D-SEC-9 item 2). May do everything, including
the local-only actions in D-SEC-9 item 3: restore, export, quit, setup and downloads, changing
keys, online models, routing, notification and search destinations, and turning confidential mode
off.

**A key holder.** A non-loopback request with a valid `X-Kestrel-Key` header or cookie. The key is
compared in constant time (`auth.py` `key_is_valid`, `hmac.compare_digest`). May browse, score with
the configured setup, apply, keep notes and use the MCP read tools (D-SEC-10 item 7). May not
restore, clear, download the whole database, change keys, models or routing, switch the LAN or MCP
on, or see balances, provider data or host paths (D-SEC-9 items 1 to 4). Enforced by `require_local`,
`LOCAL_ONLY`, `LOCAL_DOWNLOAD` and `refuse_unless_local` in `auth.py`; `needs_local` in
`kestrel/web/settings_io.py` (a local-only key, anything inside one, and any section or parent
that contains one, D-SEC-16; confidential mode off is judged on the effective value); and `kestrel/services/viewer.py`, which hides money and provider data
from remote page views.

**Everyone else.** No valid key gets 401 on every route except `/health` and `/auth`
(`auth.py` `_EXEMPT_PATHS`). With no key generated yet, remote requests are refused (fail closed).
The MCP port applies the same rule. `/docs`, `/redoc` and `/openapi.json` are disabled on it (D-SEC-10
item 7).

## 4. What leaves the computer, and when consent is asked

- **Local AI (Ollama on this computer).** Nothing leaves (D-PRIV-143).
- **A model server on your own network.** A literal private address (10/8, 172.16/12, 192.168/16,
  IPv6 unique-local or link-local) is the local network. It runs without Hosted mode or the consent
  dialog, for any server type (`kestrel/netloc.py`; `online_consent.backend_is_off_machine`;
  D-LLM-15). Host names, including `*.local`, count as the internet (D-LLM-15 amendment). No DNS
  lookup decides this.
- **An internet model server** (OpenRouter, Anthropic, OpenAI, or any other). Text is sent only
  when Hosted mode is active, the person confirmed the model and destination for that action, and
  no employer hold applies (`online_consent.consent_problem`; D-LLM-15). The dialog states what
  will be sent before the person confirms (CLAUDE.md, Privacy).
- **Tools on an online request.** Kestrel offers the model no tools it can act with. On the Messages
  protocol with the openmodel and generic backends, the answer format is sent as one required tool
  that holds only the reply schema, so the reply comes back as structured text
  (`kestrel/llm/client.py` `build_messages_body`; D-SEC-12, pinned by
  `tests/test_model_output_boundary.py`). Every other protocol sends no tool.
- **Notices, searches and update checks** go only to destinations this computer set
  (`notify.webhook_url`, `notify.ntfy_topic`, `research.searx_url`, `updates.feed_url`; D-SEC-10
  item 8).
- **Embeddings** run on the bundled in-process model (`kestrel/llm/inprocess_embed.py`). D-SEC-10
  item 1 states that the in-process engine never leaves the computer. See Not verified.

## 5. Restore rules

A backup is hostile input (D-SEC-10). The restore and its preview are local-only (D-SEC-9 item 3;
D-SEC-11 item 3). Invariant: nothing in a backup can change how this computer runs (local-only
settings, keys, consent, schema objects, queued work, model files) beyond the data rows a person
would expect to get back, and a restore never leaves work running against the wrong database.
In `kestrel/services/backup.py`:

- Restore is an import, never a file swap of the backup's database (D-SEC-20). The staged database
  is copied to a temp work file with its triggers and views dropped; a backup from an older Kestrel
  is migrated to head there (refused with the fixed sentence, nothing changed, whatever the failure
  raised; D-SEC-23), a newer one is refused.
  The work copy is then opened read-only and only read (`_work_copy`).
- A new database is built in the data folder from Kestrel's own migrations, the path a fresh
  install takes. Rows are copied for the tables in `RESTORED_TABLES` only, only where the backup
  has the table as a real table (never a view or a virtual table), and only for the columns OUR
  schema has (`_import_rows`). Unknown tables, views and triggers are never copied; the result
  counts them in plain words. The task queue is never restored (the one exception: the newest
  finished follow-up draft per application, as a `done` row cut to the fields the dialog reads), and the setting rows that stand
  for queued work or a one-time token (`run_model.pending.*`, `pending_run_mode`,
  `setup.rebuild_owed`, `setup.recheck_owed`, `undo.*`, and the queue bookkeeping
  `pipeline.orphan_requeue`, `worker.stop_request`, `scheduler.scan_hold_until`,
  `org_summary_hold.*`, D-QUEUE-10) are dropped (`_drop_markers`). So no
  restored task can pull or rebuild a model, run on a model the backup chose, or release a consent
  hold.
- The rules below run on that new database. It is then checkpointed and synced, the current
  database is snapshotted to `kestrel.db.pre-restore-<UTC>`, and the new database is written into
  the live one through SQLite (`_write_into_live`, safe with the app's connections open). The temp
  file is removed whatever happens.
- Before the swap, the Settings restore and the in-app reset stop the worker and the online lane
  with the quit's bounded logic (`app.work_loops_stopped`: the quit grace, a cancel, the lease
  hand-back) and start them again afterwards; when a loop ignores its cancel, nothing changes.
  Only those two loops' leases are handed back; the setup loop's model download or build keeps
  its lease and finishes once (D-QUEUE-10). The Settings restore checks the upload
  (`check_backup`, the same checks in a temp folder) before it stops anything, so a refused
  backup stops nothing.
- A failure after the database was written (copying config files or documents) is reported as
  what it is: the database restored and the named parts not (`RestoreResult.parts_failed`), and
  the caches are reset as after any restore (D-SEC-23).
- Config files are installed only under the names in `CONFIG_FILES`, matched after folding case
  and dropping trailing dots and spaces; settings.yml in any spelling goes through the local-only
  merge, and the preview reads the same entry (`config_plan`). Any other config entry is skipped
  and counted.
- Documents land only as `documents/<sub>/<name>.pdf` or `prep_packs/<name>.pdf`, confined to the
  data folder by `paths.confined_file`. Anything else is skipped and counted (`DOC_SUBDIRS`; D-SEC-10
  item 2).
- Stored file paths that point outside the data folder are cleared to NULL. The files are not
  touched (`_scrub_stored_paths`; D-SEC-8, issue 150).
- Learned reranker models are deactivated, and their file paths cleared, before data is installed
  (`_deactivate_rerankers`; D-SEC-9, issue 173). Scoring then falls back to the LLM tier.
- No task is restored, so no restored task can authorize a send (D-SEC-20, which replaces the
  earlier strip of consent fields from restored tasks, D-SEC-10 item 1 and item 8b).
- By default, this computer's model, Hosted mode, MCP, server, update, notice, search and identity
  settings are kept (`_keep_local_settings`, `_restore_settings_yml`; D-SEC-10 item 1, item 8a).
  A backup may turn confidential mode on, never off.
- Opt-in: after a preview lists the values, the person can tick a box to also take the backup's AI
  and connection settings (`restore_backup(restore_local_settings=...)`; D-SEC-11). Even then:
  API keys never come from a backup (D-SEC-11 amendment); Hosted-mode acknowledgments, `server.*`
  and the LAN key stay this computer's; and consent is never restored (D-SEC-11 item 2). Whether
  online sends hide the current employer (`llm.hosted_mode.redact_confidential`) also always stays
  this computer's (D-SEC-17).
- "This computer's" value is read from its effective settings (settings.yml, packaged backends an
  older settings.yml lacks, the environment and the rows): which backends carry a saved key, and
  whether confidential mode is on. A backend's `hosted.data_collection` and `hosted.zdr` always stay
  this computer's (`_live_settings`, `_PRIVACY_FIELDS`; D-SEC-18).
- Fail closed (D-SEC-19): when this computer's settings cannot be loaded, every local-only setting
  stays this computer's whatever the opt-in says, and Confidential mode counts as on unless the raw
  settings.yml, the environment and the row all say off. A backup backend whose name is not one plain
  segment (`^[A-Za-z0-9_-]+$`) is dropped; backend fields are matched by path segments. A packaged
  backend an older settings.yml lacks is merged onto its packaged entry, never written partially.
- Every setting row that touches a local-only key is flattened to its leaf keys before any of these
  rules apply, so a whole-section row (`llm`, `notify`) is filtered key by key, and the preview
  lists what it would set (`_leaf_rows`, `_regroup`; D-SEC-16).
- With the opt-in ticked, a model server that uses a key saved on this computer (`api_key_env`, or
  a real `api_key`) keeps this computer's address in both the database and settings.yml, so a
  backup can never send this computer's key to an address it chose (`_keyed_backends`; D-SEC-16).
- A source that needs its own key reads it only under a name with the adapter's own prefix
  (`USAJOBS_`, `ADZUNA_`, `SAM_GOV_`, `REMOTEROCKETSHIP_`), checked when the config is saved and
  again when the source runs, so a restored source row (or a key holder, who may not set these
  fields at all) can never send another saved key to that service (`ingest/source_secrets.py`;
  D-SEC-21).

## 6. Hostile content defenses

- **Public addresses.** A URL that a posting or a person supplies must resolve only to globally
  routable addresses. Every redirect hop is checked (`kestrel/ingest/http.py` `assert_public_url`,
  `_is_public`; D-SEC-10 item 5).
- **Pinning.** The connection goes to an address that was checked, so a second DNS answer cannot
  redirect it (`_PinnedBackend`; `tests/test_ingest_pinned_connect.py`; D-SEC-10 item 5).
- **Adapter ids.** Company board ids, the Workday tenant, data center and site are checked at config
  load and again at fetch. Each must be one path segment with no slash, query, `@` or colon
  (`kestrel/ingest/base.py` `path_segment`; `board_discovery.py` `_SLUG`; D-SEC-10 item 5).
- **CSV exports.** A text cell that starts with `=`, `+`, `-`, `@`, tab or CR gets a leading single
  quote (`kestrel/services/csv_safe.py`; D-SEC-10 item 6).
- **Model files.** A reranker pickle loads only from inside the data folder (`kestrel/learning/reranker.py`
  `load_active_reranker`; D-SEC-9, issue 173). The sklearn version is recorded, and a mismatched
  pickle is not used (`kestrel/learning/reranker.py`).
- **Signed reranker files.** Each saved reranker and its PCA file carry an HMAC-SHA256 signature
  stored in the `ml_model` row. The key is 32 random bytes in `secrets/model_signing.key`, made on
  first save and never included in a backup or restored. The signature is checked before any
  unpickling, on the same bytes that are then loaded. An unsigned file (saved before signing), a
  mismatched one, or one copied from another install is refused with a notice, and scoring uses the
  LLM tier until a new model is trained (`kestrel/learning/signing.py`, `reranker.py`
  `load_active_reranker`; D-SEC-13).
- **Model output is never a request target.** A reply is text that Kestrel checks and shows. The
  PDF renderer's browser loads only inline `data:`, `blob:` and `about:` addresses, so an address in
  markup is never fetched (`kestrel/docs/render.py`). A contact's profile link is shown as LinkedIn
  only if its host is linkedin.com and it appears verbatim in a source
  (`kestrel/research/contacts.py` `linkedin_url_verbatim`). Research fetches only stored or fixed
  hosts and takes indices, not addresses, from the model (D-SEC-12, issue 167;
  `tests/test_model_output_boundary.py`).
- **Model replies.** Every reply is schema-validated, repaired once, then quarantined with the raw
  text kept. Prose is refused (`kestrel/llm/guard.py` `run_guarded`; CLAUDE.md hard rule 3).
- **Letter promises.** Commitments such as start dates or sponsorship in a generated letter must
  appear in the person's own text (`kestrel/docs/letters.py`; D-LETTER-9).

## 7. Known limits

- **A proxy makes the address check advisory.** When httpx sends a request through a proxy, the
  proxy resolves the name, so pinning cannot apply (`kestrel/ingest/http.py` `_proxied`; D-SEC-10
  item 5).
- **Browser requests are checked by headers.** `_reject_cross_origin` refuses unsafe requests whose
  Origin is foreign, and state-changing GETs whose Sec-Fetch-Site is cross-site or same-site. A
  request with neither header is accepted as the person's. This relies on browsers sending one of
  them. No decision ID covers this check; the private decision log around line 1148 describes its reason.
- **One shared key.** Every key holder shares one secret. It has no expiry and no per-device
  identity. Rotating it in Settings replaces it; rotation is a this-computer action (`auth.py`; D-SEC-9).
- **The loopback test trusts the Host header.** A forwarder that sends a loopback Host and drops the
  proxy headers would look local. The proxy-header check catches common forwarders only
  (`auth.py` `peer_is_loopback`; D-SEC-9 item 2).
- **Software already running as you** can read the data folder directly. This model does not defend
  against it.
- **A server on your network runs without consent by design.** If another machine answers at that
  address, your text goes to it (D-LLM-15).
- **Ollama's own settings are outside Kestrel's control.** Its cloud models and update checks are
  set in Ollama (D-PRIV-143).
- **Model files come from other people.** Kestrel checks that a model answers in the right format.
  It cannot inspect how the model was trained (D-PRIV-143).
- **Promise checks match listed phrasings only.** A commitment worded differently passes
  (`kestrel/docs/letters.py`; D-LETTER-9).

## Not verified

- Whether the LAN server uses TLS, so whether the key and data are encrypted on the local network.
- That nothing in the code sends mail, submits a form or clicks apply. No repository-wide test found.
- The public promises "free" and "you are not the product" have no enforcing code.
- That generated bullets are the person's text byte for byte (CLAUDE.md hard rule 2). Not checked in
  this pass.
- Update download integrity. `kestrel/services/updater.py` declares a `sha256` field but never
  downloads or compares anything: the app only points the person at the download page. No integrity
  claim is made for the installer.
- That every embedding call stays local. Only the in-process module was read.
- Whether backups are encrypted at rest.
- The key cookie's attributes (HttpOnly, SameSite, Secure).

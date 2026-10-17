# Running Kestrel outside the desktop shell

Evaluated 2026-09-30. Sources are in `REFERENCES.md` under "Hosting outside the desktop". The
build container could not reach Cloudflare's documentation directly, so limits below come from
search summaries of the official pages and are marked where unverified.

## Decision

**Do not port Kestrel to Cloudflare Pages or Workers. Publish the local app through a Cloudflare
Tunnel with Cloudflare Access in front of it.** Both are on the free plan.

## Why a port is not feasible

Kestrel's work happens on the operator's machine, and the parts that make it useful are exactly the
parts an edge runtime cannot host:

| Kestrel needs | Workers / Pages free plan offers | Verdict |
|---|---|---|
| A worker loop and scheduler running for hours, draining LLM tasks | A per-request isolate, 10 ms CPU per request, no background process | Blocked |
| SQLite on disk via aiosqlite, Alembic, WAL | No filesystem. D1 is SQLite-compatible but a different driver and 100k row writes per day on free | Rewrite of the data layer |
| Ollama on `localhost:11434` (local GPU) | No localhost. Workers AI gives 10k neurons per day, not the tuned per-tier models, and moves inference off the machine | Blocked, and against the privacy model |
| Playwright driving Edge for PDFs | No browser process | Blocked |
| JobSpy (synchronous scraping from a residential IP) | 50 subrequests per invocation, datacenter egress that job boards block | Blocked |
| sklearn, tiktoken, structlog | Python Workers run on Pyodide: pure-Python and Pyodide-bundled packages only, 128 MB | sklearn unverified under those limits, Playwright and aiosqlite impossible |

Pages itself is in maintenance mode; Cloudflare directs new projects to Workers with static assets.
Python Workers reached general availability in 2026 and FastAPI does run there, which is why the
question was worth asking, but the app around FastAPI is what does not fit.

## The feasible path: Tunnel + Access

The app keeps running on the GPU PC, bound to `127.0.0.1` exactly as today. `cloudflared` opens an
outbound connection to Cloudflare and publishes the local port at a hostname you own. Cloudflare
Access sits in front of that hostname and requires a login before any request reaches the tunnel.
Nothing is port-forwarded and the LAN shared secret is not involved.

Requirements: a domain on Cloudflare DNS (any registrar; the free plan is enough) and the PC awake.

### Steps

1. **Zero Trust > Access > Applications > Add an application > Self-hosted.** Domain
   `kestrel.yourdomain.com`. Policy: Allow, include `Emails` = your address. Login method: add
   **One-time PIN** explicitly (new accounts default to account membership only).
2. **Zero Trust > Networks > Tunnels > Create a tunnel.** Install `cloudflared` on the PC as a
   Windows service (the dashboard gives the one-line installer with the token). Public hostname
   `kestrel.yourdomain.com` -> service `http://127.0.0.1:8765` (the port from `runtime.json`).
3. Leave `server.bind_all` **off**. The tunnel connects to loopback, so every request arrives as a
   local peer and the LAN key is not required. Do not generate one for this.
4. Add the hostname to Kestrel's allow list. Requests through the tunnel reach loopback, but they
   carry the public hostname as `Host` and, on HTMX POSTs, as `Origin`; the cross-origin guard and
   TrustedHost refuse both until the hostname is listed. Set, in `settings.yml`:

   ```yaml
   server:
     public_hostnames: ["kestrel.yourdomain.com"]
   ```

   Exact lowercase hostnames only; a wildcard or a scheme is refused at config load, because a
   wildcard here would re-open DNS rebinding on the loopback bind. Restart Kestrel. The key is
   implemented and tested against a simulated tunnel request; verifying it against a live tunnel
   is operator-required, so test one POST from the phone before trusting it.
5. Open the hostname on your phone, enter the PIN from your email, use Kestrel.

### Gotchas

- **Expired Access session and HTMX.** When the Access cookie expires, Cloudflare answers an HTMX
  request with a 302 to the login page, which HTMX swaps as HTML. Reload the page; the login flow
  runs once and everything works again. A `hx-on::response-error` reload handler is the small
  follow-up if it becomes annoying.
- **100-second responses.** Cloudflare cuts an origin response at about 100 seconds (HTTP 524).
  Page loads are fast, but the run-now button on Sources and a Tier 3 escalation can exceed it.
  The work still completes on the PC; the browser just loses the response. Refresh.
- **MCP and the extension.** The MCP port and the browser extension are loopback tools. Do not
  publish port 9110 through the tunnel. If a remote MCP client is ever needed, front it with an
  Access service token, not the email PIN.
- **The PC must be awake.** Sleep suspends the tunnel and the worker alike. Set the power plan to
  never sleep on AC or wake on a schedule before the scan cadence.
- **`cloudflared` logs the request path.** Kestrel's own access log is off by design so the LAN
  secret never lands in a log; with Access in front there is no secret in the URL, so this is
  acceptable, but treat the cloudflared log as sensitive.

### What you get

A password-protected Kestrel on any device, over HTTPS, without the plain-HTTP LAN mode and its
shared-password caveats. The privacy model is unchanged: inference stays on the PC, and the only
thing that ever leaves it is a Tier 3 payload you confirmed.

# Kestrel Capture (browser extension)

Manifest V3, for Chrome and Edge. Two features, both local-only:

- **Capture.** Click the toolbar button (or right-click → *Capture this page into Kestrel*) to send
  the current page's URL and its visible readable text to your local Kestrel
  (`POST /api/opportunities/ingest`). It works on any site, including ones with no API adapter and
  ones behind a login you are already viewing. **It reads the page you are already looking at; it
  does not crawl, does not navigate, and does not act on your behalf.**
- **Seen-badge.** On supported job boards, it collects the visible posting URLs and asks your local
  Kestrel which are already known (`POST /api/opportunities/lookup`), badging each with its score and
  status (for example `Kestrel 4.2 · apply`, or dimmed when the posting has closed, or outlined when
  it looks like a ghost job).

## What it transmits, and to where

The extension talks **only** to `http://127.0.0.1:<port>` (or the LAN host you configure in options).
It sends:

- on capture: the active tab's URL, title, and visible text, to `/api/opportunities/ingest`;
- on a supported board: the visible posting URLs, to `/api/opportunities/lookup`.

It sends **nothing to any other host**. There is no analytics, no telemetry, and no third-party
request. The `optional_host_permissions` for `http://*/*` is requested only so capture can run on an
arbitrary board you choose; the extension never contacts those hosts — it only reads the tab you
opened yourself.

## Setup

1. Load unpacked: Edge/Chrome → Extensions → *Developer mode* → *Load unpacked* → this `extension/`
   folder.
2. In Kestrel, open **Settings > LAN** and generate the **shared secret**, then paste it into the
   extension's options. Kestrel refuses a capture or a badge lookup from any extension that does
   not carry it, which is what stops another installed extension with a localhost permission from
   driving your Kestrel. It is sent as the `X-Kestrel-Key` header on real requests, never on a
   discovery probe.
3. Leave the **API port** blank and the extension finds Kestrel by itself. Kestrel claims the
   first free port from 8765-8774 and the extension probes that same range, accepting only a
   server that identifies itself as Kestrel. Set a port only to pin one specific instance; the
   running port is on Kestrel's **About** page (the `port` in `runtime.json`).
4. Click **Test connection**; it should report the running Kestrel version, the port it found, and
   whether the secret was accepted.

## Privacy

Narrowest host permissions that work (loopback by default), no background network except the two
local calls above, and this README states exactly what leaves the browser and where it goes.

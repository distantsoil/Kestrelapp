# Kestrel Capture (browser extension) 0.8

Kestrel Capture connects your browser to Kestrel on the same computer. It works only on the page
you already have open, and only when you click.

- **Capture** a job posting Kestrel does not know yet. "Capture and score now" puts it at the
  front of Kestrel's queue.
- **Read into a role.** Some roles reach Kestrel with no description because the job board showed
  Kestrel a "verify you are human" page. Open the posting in your browser, pick the role in the
  popup and click Read this page. Kestrel shows what it will save first, and refuses a page that
  looks like a check page.
- **Role summary.** On a posting Kestrel knows, the popup shows the score, why it fits, the gaps,
  and your tailored resume and cover letter (Get PDF, Copy text), plus I've applied.
- **Badges** on supported job boards: the score and fit, Applied, No description yet, Likely ghost
  or Closed. Each badge sits apart from the page and never changes it.

It never applies, fills in a form, clicks or submits anything, never gets past a login or a check
page, never reads LinkedIn pages, and talks only to Kestrel on this computer (127.0.0.1 or localhost).

## Install

From the Chrome Web Store, Microsoft Edge Add-ons or Firefox Add-ons once the listings are live.
Edge, Brave and Arc use the Chrome version.

For development, build the folders and load one by hand:

```
uv run --extra dev python scripts/build_extension.py
```

- Chrome, Edge, Brave: open `chrome://extensions` (or `edge://extensions`), turn on Developer
  mode, click Load unpacked, and choose `dist/extension/chrome`.
- Firefox: open `about:debugging`, This Firefox, Load Temporary Add-on, and choose
  `dist/extension/firefox/manifest.json`.

## Connect it

1. Open Kestrel.
2. In Kestrel: Settings, Browser extension, Copy key. This makes the connection key if there is
   none; it does not turn on access from other devices.
3. Click the Kestrel icon in the browser and paste the key. The popup finds Kestrel on its usual
   ports by itself.

Kestrel refuses changes from any other extension without the key, and every request the
extension's own routes answer needs it. Make a new key under Show details in
the same Settings section if you need to; then paste the new one into the extension.

## What it sends, and where

Only to Kestrel on this computer, `http://127.0.0.1` (the manifest also allows `http://localhost`) on
Kestrel's own port:

| When | What |
|---|---|
| A supported board page loads | The posting links on that page, to look up which Kestrel knows |
| You click Capture or Read | The page's address, title and visible text |
| You open the popup | The open tab's address, to see if Kestrel knows it |
| You click I've applied | That role's ID, to record the status in Kestrel |

The extension's Content Security Policy allows no connection to any other host, and it loads no
remote code, fonts or analytics. The font (Manrope, SIL Open Font License) is bundled.

## Permissions

| Permission | Why |
|---|---|
| `activeTab` | Read the tab you are on, only when you click |
| `scripting` | Run the one-time reader on that tab |
| `contextMenus` | "Capture this page into Kestrel" on the right-click menu |
| `storage` | Keep the connection key, port and hidden sites in this browser (`storage.local`), and Kestrel's port, switches and the hosts with waiting roles until the browser closes (`storage.session`) |
| `http://127.0.0.1/*`, `http://localhost/*` | Talk to Kestrel on this computer |

Content scripts run on Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Indeed and Glassdoor
pages to draw the badges.

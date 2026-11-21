# Kestrel Capture: store listing text

The text the owner pastes into the Chrome Web Store, Microsoft Edge Add-ons and Firefox Add-ons
(AMO). It matches the code in this folder; if the code changes what the extension can reach or
read, change this file in the same commit. Step-by-step submission: the owner's handoff guide.

## Name

Kestrel Capture

## Short description (manifest `description`, 127 of 132 characters)

Send the job posting you have open to Kestrel on your own computer. Shows Kestrel's score on job boards. Never applies for you.

## Summary for AMO (250 characters at most)

Connects your browser to Kestrel, the free job search app on your own computer. Capture a posting, fill in a role's missing description, and see Kestrel's score and your documents. Talks only to Kestrel on this computer.

## Long description

Kestrel Capture connects your browser to Kestrel, the free, local-first job search app that runs
on your own computer (kestrelapp.co). It works only on the page you already have open, and only
when you click.

What it does:
- Capture: send the job posting you are reading to Kestrel, which scores it on your computer.
- Read into a role: when Kestrel found a role but could not read its description, open the posting
  in your browser and click Read. Kestrel shows you what it will save first.
- Role summary: on a posting Kestrel knows, see its score, why it fits, the gaps, and your tailored
  resume and cover letter, ready to download or copy, so you can apply yourself.
- Badges: on supported job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Indeed and
  Glassdoor), a small badge shows Kestrel's score, Applied, No description yet, Likely ghost or
  Closed next to postings Kestrel already knows. You can turn badges off everywhere or per site.

What this extension can see and do:
- On the job boards it supports, it checks the links already on the page against Kestrel and shows
  a small badge next to postings Kestrel knows.
- When you click Capture or Read, it reads the title and text of the page you have open and sends
  them to Kestrel on this computer.
- When you open its popup, it sends the address of the tab you are on to Kestrel on this computer,
  to see whether Kestrel knows that posting and which waiting roles come from that site.
- It shows what Kestrel already knows about a role: its score, why it fits, and your documents
  for it.

What it never does:
- It never applies, fills in a form, clicks or submits anything for you.
- It never opens, reloads or edits a page, and never gets past a login or a check page. A badge is its own small element next to a link; the page's text and links stay as they were.
- It never sends anything to any server but Kestrel on this computer. Nothing is collected, sold
  or shared.
- It never reads LinkedIn pages: there it offers no Capture or Read, and shows no badges.

You need Kestrel installed and running on the same computer. Kestrel and this extension are free.
You are not the product. Source is public under PolyForm Noncommercial 1.0.0.

## Category

Chrome and Edge: Productivity (Workflow and planning on Chrome). AMO: Search Tools, or Other if the
reviewer prefers.

## Single purpose (Chrome and Edge)

Bring the job posting the person has open into Kestrel, the job search app on their own computer,
and show what Kestrel already knows about postings on job boards.

## Permission justifications

| Permission | Justification |
|---|---|
| `activeTab` | Gives access to the tab the person is on only after they click the toolbar icon or the right-click item. Opening the popup sends that tab's address to Kestrel to look it up; Capture or Read also reads its title and text. |
| `scripting` | Run a one-time function in that tab that returns its address, title and visible text. It changes nothing on the page. |
| `contextMenus` | Adds "Capture this page into Kestrel" to the right-click menu. |
| `storage` | In this browser only: `storage.local` keeps the connection key, Kestrel's port and the sites where the person hid badges; `storage.session` (cleared when the browser closes) keeps Kestrel's port for this session, its three switches and the list of hosts with waiting roles, for the toolbar count. |
| Host `http://127.0.0.1/*`, `http://localhost/*` | Kestrel is a desktop app that listens on this computer. These are the only hosts the extension talks to. |
| Content scripts on the named job boards | Draw the badges next to posting links Kestrel already knows. The script reads the posting link addresses on the page and sends them to Kestrel on this computer to look them up; it writes nothing into the page outside its own closed shadow roots. |

No optional host permissions, no `tabs`, no `downloads`, no web-accessible resources, no
externally connectable pages.

## Remote code

No. Every script is in the package. The extension pages' Content Security Policy is
`script-src 'self'` and `connect-src 'self' http://127.0.0.1:* http://localhost:*`. The font is
bundled.

## Data use (Chrome Privacy practices; Edge Privacy)

- Data types: tick **Website content** and **Web history**. The page's address, title and visible
  text are sent when the person clicks Capture or Read; the address of the open tab is sent when
  they open the popup; and the posting links on a supported board page are sent to look them up.
  All of it goes only to the Kestrel app on the person's own computer (127.0.0.1 or localhost). The developer
  never receives it.
- Certify: not sold to third parties; not used or transferred for purposes unrelated to the single
  purpose; not used or transferred to determine creditworthiness or for lending.
- Limited Use: the privacy policy states that use of this data complies with the Chrome Web Store
  User Data Policy, including the Limited Use requirements.

## Data collection (Firefox, `data_collection_permissions`)

`required: ["browsingActivity", "websiteContent"]`, built into the Firefox manifest by `scripts/build_extension.py`.
The text leaves the browser only for Kestrel on the same computer. The owner confirms this answer
on submission (whether a loopback-only send counts as "transmission" is not settled by Mozilla's
docs; declaring it is the honest reading).

## Privacy policy

https://kestrelapp.co/privacy (needs an extension section before submission; see the handoff guide).

## Support and homepage

- Homepage: https://kestrelapp.co
- Support: https://github.com/distantsoil/kestrel/issues

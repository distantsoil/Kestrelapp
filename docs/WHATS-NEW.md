# What's new in Kestrel

This page covers the current release, 0.7.0, the Release Candidate for 1.0. The full list is in
[CHANGELOG.md](../CHANGELOG.md).

## A new look

Every screen is redesigned. The window is frameless, with minimize, maximize and close in the
sidebar (macOS keeps its own buttons), and it reopens at the size and place you left it. Press
**Ctrl K** (or **Cmd K** on a Mac) to open the command palette and jump to a role, a company, a
setting or an action. Page changes, tabs, folds and saves have gentle motion; your system's
reduce-motion setting turns it off.

## Smoother

Moving between pages no longer reloads the window: the sidebar, top row and header art stay put
and only the page changes. Back and Forward work as before. If you prefer each page loaded fresh,
turn on **Classic page loads** in Settings > Preferences. The setup wizard now slides smoothly
from one step to the next.

## More places to find roles

When you add a job board search you can tick USAJobs or Adzuna (each with your own free key),
Himalayas and We Work Remotely for remote roles, and the Recruitee, Teamtailor, Personio and
Breezy HR hiring systems. You can also paste any company careers page that publishes its jobs
for search engines.

## Fewer irrelevant roles

Roles outside your search area, and roles whose titles are unrelated to the ones you target, are
dropped before they are stored or scored, so they cost no time or money. Top matches on Today
shows fully reviewed roles only.

## Alerts on your terms

Turn on **Play a sound** in Settings > Preferences to hear Kestrel's own sound with each alert it
shows, even with the window hidden. Pop-ups themselves are silent. **Quiet hours** hold pop-ups
(on this computer, by phone or by webhook) until the hours end, then send one summary.

## Safer

The libraries Kestrel bundles are updated past their known vulnerabilities, and error messages
on screen never include file paths or other internal details; the log keeps those.

## Release Candidate

0.7.0 is the Release Candidate for 1.0. The installers are not signed yet: the
[README](../README.md) explains the one-time step on Windows and macOS.

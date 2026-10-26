# Walk findings

Walked with Edge and invented data.

The 224 final images use 1440 by 900 and 1000 by 900 viewports. Full-page images are taller than the viewport when a page scrolls. No document-level horizontal overflow was detected. No application code was changed.

## 1. The existing screenshot command stops at Settings

Page: Settings.

Steps: Run the unchanged `uv run kestrel screenshots` against an isolated fictional data home. The command captures 15 light-theme pages, then waits for `button:has-text("Hosted (BYOK)")` until Playwright times out. Its temporary folder also remained until removed after the process exited.

Screenshot: [settings-computer-light.png](images/walk/settings-computer-light.png).

Expected: The capture command finds the current Settings navigation, finishes its remaining screenshots, and deletes its temporary data.

Observed: The current UI has **This computer** and online key controls on **Models**. It has no **Hosted (BYOK)** button. The old selector is stale. The new runner uses current controls and schedules cleanup after exit if Windows still holds a handle.

## 2. Tailor says a scored role has no score or posting text

Page: Tailor resume.

Steps: With the invented Northwind Freight role, open its [Evaluation tab](images/walk/role-evaluation-light.png), then [Posting](images/walk/role-posting-light.png), then Tailor resume.

Screenshot: [tailor-review-light.png](images/walk/tailor-review-light.png).

Expected: Tailor describes the 4.7 evaluation and saved posting consistently, or explains why those inputs are unavailable to resume picking.

Observed: Tailor says "This role has not been scored yet" and "No posting text is saved for this role" while Role shows a 4.7 score and the posting text. The invented seed may lack a later selection record that Tailor expects; the messages still read as contradictions to a user.

## 3. The last Applications column starts out clipped

Page: Applications board.

Steps: Open **Applications > Board** at 1440 or 1000 pixels with a card in each stage.

Screenshot: [applications-main-light.png](images/walk/applications-main-light.png).

Expected: Each stage is visible or the board clearly signals that more columns can be scrolled into view.

Observed: **Offer / Closed** and its offer cards are cut off behind the right action rail at 1440 pixels. The 1000-pixel view also starts with part of that column offscreen. Horizontal scrolling may be available, but the initial view does not make it apparent.

## 4. Some dialogs still require a manual walk

Page: Cross-page dialogs.

Steps: Review the [screenshot index](images/walk/INDEX.md) after opening the captured safe dialogs and menus: Role ask-first, Models change, Offers record, Sources add, Runs menu, Pipeline filters and saved views, Applications card menu, and Analytics export.

Screenshot: [models-change-model-light.png](images/walk/models-change-model-light.png).

Expected: A complete dialog inventory includes every state-dependent dialog, including edit, confirmation, and refusal paths reached after entering data.

Observed: The automated walk captures the listed dialogs and the missing-render error. It does not yet exercise every state-dependent edit or confirmation dialog. Those should be checked manually before treating this as exhaustive UI coverage.

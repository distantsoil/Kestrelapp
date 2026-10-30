# Settings reference

**Card summary:** Settings lets you choose what Kestrel looks for, how it runs, and how it handles your data and privacy. Adjust appearance, alerts, scoring thresholds, and other preferences from six sections. Changes save as you make them, with Undo available for most saved choices.

The defaults below describe a new install before you change settings. Platform, hardware, profile, and previous-install state can affect the value shown; those cases are marked.

## This computer

![This computer settings](../images/walk/settings-computer-light.png)

### How should Kestrel run?

| Exact label or choice | Effect | New-install default |
|---|---|---|
| **On this computer** | Runs AI models locally. | Selected unless an online model has already been configured. |
| **Online model** | Uses a hosted model. Kestrel asks before sending data. | Not selected. It stays on local models until online setup is ready. |
| **Both** | Uses local models for most roles and a hosted model for the deep read. Kestrel asks before going online. | Not selected. |
| **Online service** | Chooses the provider for the hosted model. | No universal provider default. The first saved provider is selected, or the first provider if none is saved. |
| **Service address** | Base API address for **Other** services. | Empty unless previously configured. HTTPS is required unless the service runs on this computer. |
| **Key** | API key for the selected provider. **Test key** or **Test connection** checks it; **Save key** or **Save** stores it. | No key is supplied. Testing does not save the key. |

Online setup can also be completed on [Models](models.md). Choosing **Online model** or **Both** does not itself send profile or role information.

### Setup checklist

| Exact label | Effect | Default |
|---|---|---|
| **Which kind of computer is this?** | Selects a hardware profile so Kestrel can recommend suitable local models. | Detected/recommended profile; it varies by computer. |
| **Check again**, **Download**, **Pause**, **Tune**, **Tune again**, **Run check** | Progresses through the engine, model download, model setup, and verification steps. | These are actions, not stored preferences. The available button depends on setup progress. |

**Show details** reveals the related commands. While using only an online model, the local setup checklist is marked as not needed.

### Everyday options

| Exact label | Effect | New-install default |
|---|---|---|
| **Keep this computer awake during runs** | Stops Windows from sleeping while Kestrel is open. The display can still turn off. It has no effect on macOS or Linux. | Off. |
| **Start Kestrel when my computer starts** | Adds a per-user startup entry and opens Kestrel quietly in the tray or menu bar. | Off. |
| **Keep running when I close the window** | Keeps Kestrel running in the tray after the window closes, so runs can finish. | On. Closing the window quits Kestrel when off. |
| **Check for updates automatically** | Checks for an update once a day. Kestrel does not download or install it automatically. | On. |
| **Check now** | Checks for an update immediately. | Action; no default. |

## Which roles you see

![Which roles you see settings](../images/walk/settings-roles-light.png)

**Track** switches the view between **Employment** and **Consulting**. The new-install default is Employment. Existing installs that used consulting work can retain both tracks. Turning off a track hides its work; it does not delete it. The same role cutoff numbers are used for both tracks, while the preview reflects the selected track.

### Rules that hide roles

Rules are checked before scoring. Each rule is on by default. When Kestrel is uncertain, it keeps the role. Rules that refer to profile information use values from [Profile](profile.md).

| Exact label | Effect | Default |
|---|---|---|
| **Pay below your minimum** | Hides employment roles below your minimum pay. | On; minimum is set in Profile. |
| **Needs visa sponsorship** | Hides roles that say they cannot sponsor a visa. | On. |
| **Needs a security clearance** | Hides roles that require a government clearance. | On. |
| **Titles you skip** | Hides employment roles matching titles you list in Profile. | On; no titles are skipped until you add them. |
| **Below your level** | Hides roles below your seniority floor from Profile. | On; if no floor is set, any level is allowed. |
| **Outside your locations** | Hides roles outside the locations in Profile. | On; if no locations are set, every place is allowed. |
| **Older than [number] days** | Hides employment roles posted longer ago than the number shown. | On; **45 days**. The allowed range is 1 to 365. |
| **Rate below your minimum** | Hides consulting opportunities below your minimum day rate. | On; minimum is set in Profile. |
| **Too short (under [number] weeks)** | Hides consulting projects shorter than the duration shown. | On; uses the consulting profile value, displayed in whole weeks. If none is set, the Settings display falls back to **2 weeks**. |
| **Sectors you skip** | Hides consulting opportunities in sectors listed in Profile. | On; no sectors are skipped until you add them. |
| **Older than [number] days** | Hides consulting postings older than the number shown. | On; **120 days**. The allowed range is 1 to 365. |

The visible numeric labels include **Older than** and **Too short (under … weeks)**. A rejected number is explained beside its control, and Kestrel keeps using the previous value. **See them** opens roles hidden by a rule; **Show it anyway** returns a chosen role to Pipeline.

### Score cutoffs and hidden companies

| Exact label | Effect | Default |
|---|---|---|
| **Strong match from** | Sets the score at or above which a role is a strong match. | **4.0** out of 5.0. |
| **Worth a look from** | Sets the lower boundary for roles worth a look. | **3.0** out of 5.0, and must stay lower than Strong match. |
| **Restore defaults** | Resets both cutoffs. | Action; resets to 4.0 and 3.0. |
| **Hidden companies** / **Unhide** | Returns a company and its roles to your lists. | No companies hidden by default. |

Cutoff values accept 0.0 to 5.0 in 0.1 steps. The preview says when fewer than five scored roles are available to make a useful comparison.

## Scoring and learning

![Scoring and learning settings](../images/walk/settings-scoring-light.png)

**Track** switches the readout between Employment and Consulting. The scoring bars are read-only. Their starting weights are:

| Exact Employment label | Employment default | Exact Consulting label | Consulting default |
|---|---:|---|---:|
| **How well the role fits you** | 30% | **How well the work fits you** | 30% |
| **Your field** | 20% | **Access to the buyer** | 20% |
| **The company** | 20% | **The money** | 20% |
| **Pay and logistics** | 15% | **Can you deliver it** | 15% |
| **Career step** | 15% | **Strategic value** | 15% |

Kestrel may offer a tested weight change based on your ratings. **Accept** applies the suggestion from the next run; **Not now** dismisses it. **Restore original weights** resets the selected track's weights.

| Exact label | Effect | New-install default |
|---|---|---|
| **Uses your ratings as examples** | Lets Kestrel use your ratings as examples after at least 15 ratings. | On; the control becomes available when ready. |
| **Tunes to your taste** | Lets Kestrel tune weights after at least 30 ratings. | On; the control becomes available when ready. |
| **Your own sorting model** | Lets Kestrel build a personal sorting model after at least 200 ratings. | On; the control becomes available when ready. |
| **Export my ratings** | Downloads your rating history. | Action; no default. The employer name is included unless Confidential mode is on. |
| **Forget my ratings** | Opens a confirmation for selected learning data. | The three checkboxes, **Your ratings**, **Your own sorting model**, and **Suggested weight changes**, start checked. Type **RESET** to confirm. |

Forgetting ratings does not remove roles, scores, applications, documents, or accepted weights. **Agrees with your calls** is a readout; fewer than five ratings are reported as too few to tell. See [Calibrate](calibrate.md) for rating history.

## Privacy and access

![Privacy and access settings](../images/walk/settings-privacy-light.png)

| Exact label | Effect | New-install default |
|---|---|---|
| **Confidential mode** | Replaces the current employer name with the chosen label in list views, the JSON API, MCP tools, and generated documents. Online requests use a random stand-in code. | Off. The employer is selected in [Documents](documents.md); the suggested replacement label depends on the profile. |
| **Hide [employer] as [label]** | Sets which employer to redact and what label to show. | Employer and suggested label depend on the profile. If no current employer is selected, Kestrel cannot redact one. |
| **Access from other devices** | Allows a phone or laptop on your own network to open Kestrel using a shared key. The change takes effect after restart. | Off. Turning it on requires **I understand** and **Turn on**. |
| **Make a new key** | Invalidates the old shared key for all devices. | Action; no default. |
| **Connect an AI assistant (MCP)** | Lets a compatible assistant read your roles and applications, and make the changes the card marks **Changes your lists**: adding a role, asking for a search or a new score, moving an application to a new stage, and adding a note. The assistant is separate software with its own privacy and action behavior. | Off. |
| **Change port** | Changes the MCP connection port. | **9110**. The field is under **Show details**; allowed ports are 1024 to 65535. |

Confidential mode can show a warning if there is no confirmed employer to redact; in that state, Kestrel will not send data to an online model. Other-device access uses plain HTTP and one shared key, so use only a home network or VPN you control. The MCP service is off until enabled.

## Your data

![Your data settings](../images/walk/settings-data-light.png)

This section has no persistent preference switches. Its controls perform a specific file or data action:

| Exact label | Effect and default behavior |
|---|---|
| **Download a backup** | Creates a ZIP containing your profile, roles and scores, applications, documents, and settings. Online model keys and logs are excluded. |
| **Restore from a file** | Shows a confirmation before replacing current data. Kestrel saves a backup of the current data first. An unreadable or invalid file leaves current data in place and shows an explanation. |
| **Import past applications** | Reads a file and previews the mapped rows before adding anything. Rows selected by the preview are the default; press **Add** to import. **Undo** removes only that import. |
| **Your contacts** | Imports a connections file you selected. Email addresses are not kept. During review, **Keep them** is selected by default for contacts missing from a newer file; **Remove them** changes that choice. Press **Apply changes** to import. |
| **Download a diagnostics file** | Creates a file only when asked. Kestrel does not send it. It contains app version, recent logs, settings, and health check results, but not your profile, resumes, keys, roles, or applications. |
| **Restart setup** | Repeats first-run setup while keeping the profile, documents, and roles. |
| **Clear everything** | Requires confirmation and typing **RESET**. A backup is saved first. **Keep my online model keys** is checked by default. |

Imports show an explanation for unreadable files or refused changes. A contacts removal asks for confirmation; application imports are not written until **Add**.

## Preferences

![Preferences settings](../images/walk/settings-preferences-light.png)

### What you're looking for

| Exact label | Effect | New-install default |
|---|---|---|
| **Employment** | Shows or hides employment roles throughout Kestrel. | On. |
| **Consulting and contract work** | Shows or hides consulting roles throughout Kestrel. | Off for a new install. Existing installs can retain both tracks. |

At least one track must remain on. Turning one off hides its data without deleting it.

### Appearance

| Exact label | Effect | New-install default |
|---|---|---|
| **Theme**: **Light**, **Dark**, **Match computer** | Sets light or dark appearance in this browser. | **Match computer**, stored per browser. |
| **Color scheme** | Sets the application color tokens across browsers: **Paper and Indigo**, **Paper and Teal**, **Paper and Plum**, **Paper and Graphite**, or **High contrast**. | **Paper and Indigo**. |
| **Your picture** | Selects **Initial**, **Bird**, **Leaf**, **Star**, **Sun**, **Mountain**, **Compass**, or **Upload a photo** for the top-right account mark. **Remove photo** deletes the uploaded picture. | **Initial**, using the first name in Profile or **K** if no first name is available. |
| **Scene** / **Manage scenes** | Chooses one title-band scene or a daily rotation. When daily rotation is on, **Include [scene] in rotation** controls which scenes appear; when it is off, **Use [scene]** selects the single scene. Choices are **Mountains**, **Coast**, **Desert**, **Prairie**, **Forest**, **City**, **Lake**, **Farmland**, **Winter**, **Canyon**, **Island**, **Aurora**, **Rain**, and **Savanna**. **None** is available when daily rotation is off. | **Change scene daily** is on, with all 14 scenes in rotation. |
| **Animate the scene** | Turns scene movement on or off. | On. |
| **Seasonal touches** / **Manage touches** | Enables date-based artwork extras. The group tabs are **Seasons and nature**, **Civic**, and **Lights and lanterns**. **Show [touch]** controls individual extras: Spring, Earth Day, Longest day, Shooting stars, Autumn, Harvest, Longest night, New Year, Fourth of July, Halloween, Lunar New Year, Labor Day, Colors of spring, Crescent evenings, Lantern night, and Winter lights. | Master switch on; all groups and touches on. |
| **Text size**: **Small**, **Default**, **Large** | Changes interface text size. | **Default**. |
| **Reduce motion** | Reduces interface animation and slides. | Off. |

The scene choice and scene rotation are separate: the new-install rotation includes all scenes, and the current scene changes daily. Seasonal-touch selections remain saved when the master switch is off.

### Region

| Exact label | Effect | Default |
|---|---|---|
| **Country** | Displays the supported country. | **United States**, fixed and not editable. |
| **Currency** | Displays the currency used for pay and offers. | **US dollars**, fixed and not editable. |
| **Date format** | Changes how dates appear. Choices are **MM/DD/YYYY**, **MM/DD/YY**, **DD/MM/YYYY**, **DD/MM/YY**, **YYYY-MM-DD**, and **Oct 23, 2026** (stored as Mon D, YYYY). | **MM/DD/YYYY**. |
| **Time**: **12-hour**, **24-hour** | Changes how times appear. | **12-hour**. |

### Notifications

| Exact label | Effect | New-install default |
|---|---|---|
| **New strong matches** | Alerts for roles that score high for you. | On. |
| **Follow-ups due** | Alerts for applications waiting on your next step. | On. |
| **Run finished** | Alerts when a search run finishes. | On. |
| **Something needs attention** | Alerts about a source, model, or backup problem. | On. |
| **Bell in Kestrel** | Keeps alerts in the in-app bell. | Always on; cannot be turned off here. |
| **Computer notifications** | Allows small pop-ups from your computer. | On. |
| **Play a sound** | Saved preference. Alerts do not play sounds yet; **Play** previews the chosen sound. | On. |
| **Sound** | Chooses **Chime**, **Soft click**, or **Bell**. | **Chime**. |
| **Volume** | Sets the volume of the sound preview from 0% to 100%. Alerts do not use it yet. | **60%**. |
| **Quiet hours** with **From** and **To** | Saved, but not active yet. The times do not mute pop-ups or sound. Alerts always appear in the bell. | Off; **From 9:00 PM** to **To 7:00 AM**. |

## Saving, Undo, and errors

Settings save as you make each change; there is no page-wide Save button. Most successful changes show a status message and an **Undo** action. Undo applies only while the saved value is still the current value; if a later change prevents a safe reversal, Kestrel says **This can no longer be undone**. The theme is stored for the current browser, and the sound-volume slider saves quietly while you drag it.

Invalid values, unavailable actions, failed saves, and unreadable files produce a plain explanation near the control or in the action's result area. A refused setting remains at its previous value. Track selection refuses to turn off both tracks. Restore, reset, forgetting ratings, and removing contacts use confirmation steps; irreversible data actions ask for **RESET** where shown.

## Related guides

[Models](models.md) · [Profile](profile.md) · [Documents](documents.md) · [Applications](applications.md) · [Calibrate](calibrate.md) · [Sources](sources.md) · [Help](help.md)

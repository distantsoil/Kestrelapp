# Calibrate: teach Kestrel your judgment

Use Calibrate to rate roles that Kestrel is unsure about. Choose Poor through Great before seeing Kestrel's score; optional reasons and score nudges explain your call. Your ratings help improve scoring over time, and Kestrel asks before applying any proposed weight change.

![Employment calibration queue](../images/walk/calibrate-main-light.png)

## Rate a queued role

Calibrate presents up to ten evaluated roles per session, one at a time. Kestrel selects roles where its scoring signals disagree, or roles unlike the ones you have already rated. **Why Kestrel is asking** gives a short reason for the current role.

The card shows the title, organization, place, pay, and a posting excerpt. Choose **More details** to read the full posting in a panel. If Kestrel has the original posting URL, **Open original posting** opens it in your browser.

Calibrate rates how well a role or engagement matches you, not your resume or capability statement. Your resume and other documents are on [Profile](profile.md), under **Documents**.

Under **Good match?**, choose one rating:

- **Poor** is 1.
- **Weak** is 2.
- **Maybe** is 3.
- **Good** is 4.
- **Great** is 5.

Kestrel keeps its evaluation hidden until you choose a rating. It then shows what you said, Kestrel's score out of 5, the score for each dimension, and Kestrel's short read. **Close** means the two scores are less than half a point apart; otherwise, the result is **Different**. If Kestrel has no score for the role, the page says so.

Choose **Save** to record your rating and move to the next role. Choosing a rating alone is enough to save; the save button reads **Save, nothing to add** until you add a reason or a score nudge.

### Add a reason or adjust a score

After you choose a rating, select **Tell Kestrel why** to open optional feedback. Choose any reason chips that apply, then use the minus or plus button beside a dimension to lower or raise Kestrel's score by one point. Each score stays between 1 and 5. The reasons and score changes are optional, and there is no free-text note field. When you have added a reason or a score change, the save button reads **Save with reasons**.

The reason chips depend on the active track:

| Employment | Consulting |
| --- | --- |
| Pay | Rate |
| Location | Scope |
| Level | Buyer |
| Kind of work | Timing |
| Company | Delivery risk |
| Other | Other |

Employment scores **Role fit**, **Domain match**, **Company context**, **Pay and logistics**, and **Career direction**. Consulting scores **Engagement fit**, **Buyer access**, **Economics**, **Can you deliver it**, and **Strategic value**. Only dimensions Kestrel scored appear in the card.

The number keys **1** through **5** select Poor through Great. Press **O** to open or close the reason panel, **S** to skip, and **Enter** to save when focus is not in a form field, button, or link. You can also use the mouse or keyboard on every control.

### Skip

Choose **Skip** to move the role to the end of this session's queue. Skipping does not create a rating. The skip list lasts for the current session, so opening Calibrate again starts a new queue.

## Change an earlier rating

**Learning history** lists saved ratings, newest first, eight per page. Each row shows when you rated the role, what you said, Kestrel's score, and the result: **Close**, **Different**, or **Changed**, which marks a rating you have corrected. Use **Newer** and **Older** to move between pages.

Choose **Change** beside a role to load its earlier rating into the card. The card shows **Changing an earlier rating. You said** your earlier word. Choose another rating or add a reason or score nudge, then choose **Save**. Choose **Cancel** on the card to leave the earlier rating as it was.

Kestrel labels a comparison based on fewer than five ratings **Not enough data yet**, next to the count, and does not present a small sample as a reliable pattern. With no saved ratings, history says **Nothing yet. Your ratings show up here.**

## What your ratings change

The progress strips at the top show this session, the scoring tune-up, and your taste. The milestones in the app are 15 ratings, when Kestrel can show the AI a few of your rated roles as examples for similar roles; 30 ratings, for a possible weight tune-up; and 200 ratings, for training a sorting model. The strips show the milestones currently set for your app.

Calibrate itself does not call a model. Scoring that uses your examples follows the consent and model settings for that scoring action. See [Models](models.md) for model choices.

### Weight tune-up

At the tune-up milestone, Calibrate may show **Weight tune-up ready**. The card compares the current and proposed weights for each score, and shows how far each would miss your ratings on ratings Kestrel did not use to suggest them, in points on the five-point scale. Review the values, then choose **Use the new weights** or **Keep current**. The change is never automatic. Accepted weights apply from the next scoring run.

### Sorting model

At the sorting-model milestone, Kestrel trains a model on this computer and checks it against ratings it did not train on. It uses the learned model only if it passes every check. **Learned, not in use yet** means the usual scoring still gets more of your ratings right, or a check did not pass. Kestrel tries again after 50 more ratings. If the model is in use, **Your taste** says **In use**.

Choose **Show details** to see the checks, including the current and required values, which checks pass, the last training time, and the next step. **Not promoted** means the rule-based quick screen remains in charge for this track.

**Train sorting model** starts local training. While training is queued or running, the page checks for an update every two seconds and the button is disabled. When training finishes, Calibrate says whether the model passed its checks and was selected for scoring. If it did not pass, the current scoring stays in use. **Show training details** expands the reason when one is available. If the setting is off, the button is disabled and the page links to [Settings](settings.md), under **Scoring and learning**. If training cannot finish, the page says **Training could not finish. Try again.** If the status panel cannot be built, it says **Promotion status unavailable** and the rating queue still works.

## Empty queue and other messages

When there are no roles to rate, the card says **Nothing to rate right now.** If Kestrel is still unsure about more roles, choose **Keep going** to start another session. Otherwise, the card says Kestrel is not unsure about anything right now and finds more after the next run. **Change an earlier rating** jumps to the history. **Back to Pipeline** opens [Pipeline](pipeline.md).

After you rate everything in a session, the card says **All N rated.**

## Consulting track

When Consulting is enabled, use the track switch at the top of the sidebar to change from Employment to Consulting. Calibrate keeps the queues and learning progress separate by track. Consulting uses the same rating scale and controls, with its own score dimensions and reason chips listed above.

See [Profile](profile.md) for Consulting setup and [Analytics](analytics.md) for the results of each track. For help with the Calibrate page, select **How it works** on the page or open [Help](help.md).

## Related guides

- [Pipeline](pipeline.md) explains how Kestrel finds and screens roles or leads.
- [Models](models.md) explains which model scores roles and where it runs.
- [Settings](settings.md) explains scoring and learning preferences.
- [Profile](profile.md) explains Employment and Consulting setup.

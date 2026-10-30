# Model check: compare models against your ratings

Use Model check to compare the models assigned to Quick screen and Rating against roles with known verdicts. Rate at least five roles in Calibrate to start a check, then review its recommendation before changing a model. Deeper read and online comparisons are optional; online checks name what they send and require your confirmation.

![Model check](../images/walk/model-check-main-light.png)

## Before you start

Open **Models**, then choose **Check my models**. The check needs at least five saved role ratings. If the button is disabled, the card shows how many ratings you have and links to [Calibrate](calibrate.md). See [Models](models.md) to check which models are assigned to each step or to connect an online provider.

Until you choose **Use my ratings as the answer key**, Kestrel scores the models against shipped example roles. Once you have enough ratings, choose that button to build an answer key from your own judgments. It changes to **Update the answer key from my ratings** after you have built one. The line above the button says which answer key the next check will use.

Local models need the AI engine to be running and the models available. To test online models, connect a provider under **Models** and complete its online-use approval. If confidential mode is on but no current employer is selected, Kestrel holds online checks and explains why; mark your current employer on [Documents](documents.md), or choose **Choose your current employer** under **Settings**, **Privacy and access**, then try again.

## Choose what to check

The main card shows how many roles you have rated. Select **Check my models** to compare Quick screen and Rating. Kestrel scores each model against the same answer key and shows progress on this page. The estimate is approximate and depends on the computer, number of roles, and models being tested.

You can change these options before starting:

- **Also check Deeper read** adds that step. Deeper read sends the whole posting to the model, so a paid model uses more credit for this check.
- **Include online models ([provider])** adds online model comparisons. The page lists the data each step sends. The online check tries a free and a paid model; the paid model uses your credit. Without this option, the check tests models on this computer. If a model already assigned to a step runs online, Kestrel asks you to include online models before it sends the rated roles.
- **Use my ratings as the answer key** or **Update the answer key from my ratings** makes your saved judgments the answer key for later checks. Until then, the check uses shipped examples.

An online check starts only when the connected provider, key, hosted-use approval, and the page's confirmation all pass. If a requirement is missing, the card explains what to fix and does not start the check. When confidential mode is on and an employer is selected, Kestrel replaces that employer with a code before sending; if no employer is selected, the send is held.

## Follow a check in the background

After you start, the card shows **Waiting to start** or **Checking [step] with [model]**, a progress bar, and the completed count. The card refreshes while the check runs. You can leave Model check or restart Kestrel; the task resumes after a restart. Choose **Cancel** to stop it before the next model.

Before the first finished check, the result card says **No check yet**. If you cancel a run, the card confirms **The check was cancelled.** and you can start another check when ready.

If a check stops before finishing, the card says **The last check stopped before it finished. Press Check my models to try again.** Open **Show details** to see the error, then retry. If another check is already running, the page says **A model check is already running.** Let it finish or cancel it before starting another.

## Read the recommendation

The **Recommendation** card shows when the check ran, which answer key it used, and how many roles were in that key. Kestrel recommends a change only when a tested alternative clearly improves on the model in use. It prefers a model on this computer. An online model is recommended only when it improves on the best local option by at least five matches per 100 answer-key roles.

For a recommended change, the card names the step and model and says how many more roles it matched per 100. It may also say whether the model is slower or faster, where it runs, and whether it uses credit. If an online model is recommended, the card explains what would be sent and asks you to confirm before switching. If confidential mode held an online candidate, the card identifies it and explains that nothing was sent.

Choose **Switch [step] to [model]** to make one recommended change, or **Make [n] changes** to apply several. When several changes are recommended, a step-specific **Switch [step]** button also applies only that change. A step already using the recommended model says **In use now.** A local switch takes effect without restarting. For an online switch, first check the box that says what will be sent to the provider and that it uses your credit. If the provider is disconnected, approval is missing, or the model is no longer available, Kestrel shows a refusal message and leaves that step unchanged.

After a successful switch, **Undo** puts the affected step or steps back as they were. Undo is available only while the saved change is still current. If a later change replaced it, Kestrel says **This can no longer be undone.** You can also change a model from [Models](models.md).

When no alternative is clearly better, the card says to keep the current model. Steps with too few roles are listed as too few to compare. If no step has enough data, the card says **Too few rated roles to check your models yet.** It then says to rate at least 5 roles in Calibrate and check again.

## See the numbers and earlier checks

Open **See the numbers** for one table per step. Each table reports:

- **Matches the answer key**: how often the model reached the answer key's verdict. Quick screen compares keep or skip; Rating and Deeper read compare their verdicts.
- **Answers in**: approximate response time in seconds.
- **Readable the first time**: how often the first model response met the required format.
- **Where**: whether the model ran on this computer or online.

Each table states how many roles the result uses. Fewer than five is shown as **Too few roles to judge**, not as a percentage. A model that could not be tested says **Could not be tested**. The measures appear in separate tables because the steps answer different questions.

**Earlier checks** lists past results by date, the steps checked, the answer-key size, and the summary. Open **Advanced: pick a step and models yourself** to see **Earlier comparison from your stored answers**. That separate table compares saved evaluations with your ratings and reports its own sample count, exact-verdict match, and mean absolute score error. With no matching evaluations and ratings, it says **Nothing to compare yet. No role has both a stored answer and your rating.** Counts under five are marked too few.

## Advanced controls

Open **Advanced: pick a step and models yourself** to test a particular alternative or assign one to a step.

Under **Check one step against one model**, choose:

- **Step**: Quick screen, Rating, or Deeper read.
- **Where it runs** and **Model**: choose from the listed local models or the connected provider's hosted models. The check compares the chosen model with the model currently assigned to that step.
- **Kestrel's instructions to the AI**: **Current (came with the latest update)** uses the current instructions. **The version before it** is not available yet in this build. It is greyed out, and every check uses the current instructions.
- **Check this one**: queues the comparison in the background. Online candidates have the same approval and confirmation requirements as the main check.

If hosted models are not listed, choose **Load hosted models** to fetch the connected provider's list. If loading fails, an inline message explains the problem; you can choose **Load hosted models** again to retry. If the AI engine is unavailable, the page warns that local models cannot be listed; online models may still be available. If no online provider is connected, the page does not offer the online-model checkbox or hosted models.

Under **Use a model for a step**, choose a **Step** and **Model**, then select **Use this model** to assign it without running a comparison. The confirmation message names the saved change. A hosted choice is stored as that step's override and appears as **Override** on Models. This control does not send role text; any later online work still follows the usual approval and confirmation requirements.

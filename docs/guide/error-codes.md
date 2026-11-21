# Error codes

Each code below names one problem Kestrel can report, such as `STA-006`. Find the code to see what happened and what you can do. A code never changes meaning and is never reused.

This page is generated from Kestrel's list of codes by `scripts/error_codes_table.py`. Do not edit it by hand.

## Starting Kestrel (STA)

| Code | What happened | What to do |
|---|---|---|
| <a id="sta-001"></a>STA-001 | **The last start is still closing.** Kestrel's local server from the last attempt was still closing, so Kestrel did not start a second one. | Wait a moment, then choose Try again. |
| <a id="sta-002"></a>STA-002 | **Kestrel did not start twice in a row.** Kestrel could not start the last two times. | Choose Start with optional features off. Your roles, settings and files stay as they are. |
| <a id="sta-003"></a>STA-003 | **Your data is from a newer version.** Your data was saved by a newer version of Kestrel than the one you opened. | Choose Download latest and install it, then open Kestrel again. Your data stays as it is. |
| <a id="sta-004"></a>STA-004 | **Kestrel could not open your data.** Kestrel could not open your data on this computer while it was starting. | Choose Try again. If it stops again, choose Show details to see the step that stopped, and include the log file from Open logs folder when you ask for help. |
| <a id="sta-005"></a>STA-005 | **Another app is using the connection.** Another app was using the connection Kestrel needs on this computer. | Close other apps, then choose Try again. |
| <a id="sta-006"></a>STA-006 | **The local server did not answer.** Kestrel's local server did not answer in time, or stopped while it was starting. | If the card says the server did not answer in time, choose Try again. If it says the server stopped while starting, fix the cause named in Show details first, then choose Try again. |
| <a id="sta-007"></a>STA-007 | **The last session is still closing.** A relaunch waited a minute, but the last Kestrel session had not finished closing. | Wait a moment, then choose Try again. |
| <a id="sta-008"></a>STA-008 | **The start was canceled.** You chose Cancel while Kestrel was starting, so it stopped on purpose. | Choose Try again to start Kestrel, or Quit to close it. |

## AI models (MDL)

| Code | What happened | What to do |
|---|---|---|
| <a id="mdl-001"></a>MDL-001 | **The AI request did not finish.** A request to an AI model did not finish, and Kestrel could not tell the exact cause from the kind of failure. | Try again. If it keeps happening, run the checks on the Models page and include the details when you ask for help. |
| <a id="mdl-002"></a>MDL-002 | **The local AI stopped while loading.** The AI engine on this computer stopped while it was loading the model. | Restart Ollama, then try again. If it keeps happening, pick a smaller model on the Models page. |
| <a id="mdl-003"></a>MDL-003 | **The AI took too long to answer.** A request to an AI model waited longer than Kestrel allows, so Kestrel stopped waiting. | Try again. A large model on a busy computer can be slow, and a smaller model on the Models page answers sooner. |
| <a id="mdl-004"></a>MDL-004 | **Kestrel could not reach the AI.** Kestrel could not connect to the service that runs the AI model. | If you use models on this computer, open Ollama. If you use an online model, check that this computer is online. Then try again. |
| <a id="mdl-005"></a>MDL-005 | **The AI service asked Kestrel to slow down.** The service that runs the model said it had too many requests and asked Kestrel to wait. | Wait a minute, then try again. |
| <a id="mdl-006"></a>MDL-006 | **A model download or build stalled.** The AI engine on this computer sent no progress on a model download or build for a long time, so Kestrel stopped waiting. | Choose Try again. If it stalls again, check that this computer is online and that Ollama is still open. |
| <a id="mdl-007"></a>MDL-007 | **The local AI engine reported an error.** The AI engine on this computer answered a model download or build with an error of its own. | Read the message beside the code, check the model name and the free disk space, then choose Try again. |

## Online models and consent (ONL)

| Code | What happened | What to do |
|---|---|---|
| <a id="onl-001"></a>ONL-001 | **The online service did not accept the key.** The online service said the API key Kestrel sent is not valid or not allowed. | Check the key for that service under Settings, Hosted, then try again. |
| <a id="onl-002"></a>ONL-002 | **The online account cannot pay for this request.** The online service said the account is out of credit, over its limit, or short of the credit this request needs. | Add credit or raise the limit with the provider, then try again. |
| <a id="onl-003"></a>ONL-003 | **Nothing was sent: the send was not confirmed.** Kestrel sent nothing online because the dialog that says what will be sent was not confirmed. | Open the dialog again, read what would be sent, tick the box and confirm. |
| <a id="onl-004"></a>ONL-004 | **Nothing was sent: the destination changed.** Kestrel sent nothing online because the model or online service changed after the dialog opened. | Press the button again to see what would be sent now, then confirm. |
| <a id="onl-005"></a>ONL-005 | **Nothing was sent: confidential mode held it.** Kestrel sent nothing online because confidential mode is on and no current employer is chosen. | Choose your current employer in Settings, then try again. |
| <a id="onl-006"></a>ONL-006 | **Nothing was sent: Hosted mode is off.** Kestrel sent nothing online because online models need Hosted mode, which is off. | Turn on Hosted mode under Settings, Hosted, or pick a model on this computer. |

## Job sources and searches (SRC)

| Code | What happened | What to do |
|---|---|---|
| <a id="src-001"></a>SRC-001 | **This source's setup is not valid.** Kestrel could not use how this source is set up, so it did not check it. | Open the source, fix its setup, then check it again. Remove it and add it again if you are not sure what is wrong. |
| <a id="src-002"></a>SRC-002 | **The site asked Kestrel to slow down.** The site said it was getting too many requests, so this check stopped early. | Do nothing. Kestrel tries again on the next check and waits longer between requests. |
| <a id="src-003"></a>SRC-003 | **The site had a server problem.** The site answered with a server error, so this check stopped early. | Do nothing. Kestrel tries again on the next check. If it keeps happening, the site may be down. |
| <a id="src-004"></a>SRC-004 | **The site took too long to answer.** The site did not answer in time, so this check stopped early. | Do nothing. Kestrel tries again on the next check. Check your internet connection if every source does this. |
| <a id="src-005"></a>SRC-005 | **The site does not allow reading this page.** The site's rules ask programs not to read this page, so Kestrel did not open it. | Open the posting in your browser instead, or turn this source off. |
| <a id="src-006"></a>SRC-006 | **This source needs a key of your own.** This board needs a key of your own, and it is missing or the board turned it down. | Open Sources, find the board under Sources that need your own key, and save a key the board accepts. |
| <a id="src-007"></a>SRC-007 | **The daily limit for this source is used.** This source allows only a few checks a day, and today's are used. | Do nothing. Kestrel checks again tomorrow. |
| <a id="src-008"></a>SRC-008 | **The page was not found.** Kestrel could not find this page or board at the saved address. It may have moved. | Open the site in your browser and add the source again from its current address. |
| <a id="src-009"></a>SRC-009 | **Part of the check did not finish.** This check stopped before it read everything the source offered. | Do nothing. Kestrel tries again on the next check. |
| <a id="src-010"></a>SRC-010 | **Kestrel could not read this source.** The last check of this source did not work. | Do nothing. Kestrel tries again on the next check. If it keeps failing, remove the source and add it again. |
| <a id="src-011"></a>SRC-011 | **A posting could not be saved.** Kestrel read a posting from this source but could not save it. | Do nothing. Kestrel tries again on the next check. The other postings were saved. |
| <a id="src-012"></a>SRC-012 | **A source hit a problem Kestrel did not expect.** Something unexpected stopped the check of this source. | Do nothing. Kestrel tries again on the next check. If it keeps happening, use Show details when you ask for help. |
| <a id="src-013"></a>SRC-013 | **A search has no job boards left.** This search named only a board that Kestrel does not search, so it was turned off. | Open the search and choose Use Kestrel's default boards. |
| <a id="src-014"></a>SRC-014 | **No posting could be saved.** Kestrel read the postings from this source but could not save any of them. | Do nothing. Kestrel tries again on the next check. If it keeps happening, use Show details when you ask for help. |

## Runs and scoring (RUN)

| Code | What happened | What to do |
|---|---|---|
| <a id="run-001"></a>RUN-001 | **The run did not run.** No search could run, so the run ended without checking any source. | Do nothing. Nothing was lost and the searches run again. Choose Run now to try sooner. |
| <a id="run-002"></a>RUN-002 | **Kestrel closed before the run ended.** Kestrel was closed or stopped while this run was still going. | Do nothing. Nothing was lost. Choose Run now to start a new one. |
| <a id="run-003"></a>RUN-003 | **A search was cut off.** Kestrel was closed while this search was still going, so it did not finish. | Do nothing. The search runs again on the next run. |

## Health checks (CHK)

| Code | What happened | What to do |
|---|---|---|
| <a id="chk-001"></a>CHK-001 | **Python is the wrong version.** Kestrel's check found a Python version it does not support. | Install the Python version the check names, then run the check again. |
| <a id="chk-002"></a>CHK-002 | **The uv tool was not found.** The check could not find the tool Kestrel uses to install its parts. | Install uv from its own website, then run the check again. |
| <a id="chk-003"></a>CHK-003 | **A settings or rules file is not valid.** The check could not read one of Kestrel's settings or rules files. | Fix the line the check names, or restore the file from a backup, then run the check again. |
| <a id="chk-004"></a>CHK-004 | **Your profile is missing or incomplete.** The check could not find a finished profile. | Open Profile and fill in the missing parts, then run the check again. |
| <a id="chk-005"></a>CHK-005 | **The AI engine is not running.** The check could not reach the AI engine on this computer. | Start Ollama, then run the check again. |
| <a id="chk-006"></a>CHK-006 | **A model is not downloaded.** The check found a model that Kestrel needs but this computer does not have. | Open Models and download the missing model, then run the check again. |
| <a id="chk-007"></a>CHK-007 | **A model has too little room to read.** A model is set up to read less text than a step needs, so long postings would be cut short. | Open Models and choose Fix it, or pick a model with more room. |
| <a id="chk-008"></a>CHK-008 | **A model failed the room-to-read test.** A model did not find a word hidden near the end of a long posting, so long postings may be cut short. | Open Models and choose Fix it, then run the check again. |
| <a id="chk-009"></a>CHK-009 | **A model does not turn thinking off.** A model keeps thinking before it answers, which slows every role. | Open Models and choose Fix it, or pick a different model. |
| <a id="chk-010"></a>CHK-010 | **A model does not answer in the right format.** A model did not return data in the shape Kestrel needs. | Open Models and choose Fix it, or pick a different model. |
| <a id="chk-011"></a>CHK-011 | **No browser was found for PDFs.** The check could not find a browser that Kestrel can use to make PDFs. | Install Microsoft Edge or Google Chrome, then run the check again. |
| <a id="chk-012"></a>CHK-012 | **The connector port is in use.** Another program is using the port that Kestrel's AI connector wants. | Close the other program, or change the connector port in Kestrel's settings. |
| <a id="chk-013"></a>CHK-013 | **An Ollama setting could be better.** An Ollama setting is not what Kestrel works best with, which can slow scoring. | Follow the suggestion beside the check, then restart Ollama. |
| <a id="chk-014"></a>CHK-014 | **The AI address is not on this computer.** Kestrel is set to use an AI engine that is not on this computer. | Open Models and point Kestrel back at the engine on this computer. |
| <a id="chk-015"></a>CHK-015 | **Ollama listens beyond this computer.** Ollama accepts connections from other computers, not only this one. | Set Ollama to listen only on this computer, then restart it. |
| <a id="chk-016"></a>CHK-016 | **A cloud-hosted model is installed.** Ollama has a model that runs on someone else's computer, not yours. | Remove that model in Ollama, or keep it if you chose it on purpose. |
| <a id="chk-017"></a>CHK-017 | **Ollama made a connection you did not expect.** During the check, Ollama connected to something besides this computer. | Open the details of the check to see what it was. Turn off Ollama's cloud features if you did not choose them. |
| <a id="chk-018"></a>CHK-018 | **A model is not one Kestrel tested.** A model on this computer differs from the version Kestrel tested. | Download the model again from Models, or keep it if you chose it on purpose. |
| <a id="chk-019"></a>CHK-019 | **No contact is set for reading job sites.** The contact line Kestrel sends to the sites it reads is still a placeholder. | Add your real contact email to the contact line in Kestrel's settings, then run the check again. |
| <a id="chk-020"></a>CHK-020 | **A company page did not look as expected.** The check read a company page and found it laid out differently than Kestrel expects. | Check that source on Sources, or remove it and add it again. |
| <a id="chk-021"></a>CHK-021 | **Your data could not be fully checked.** The check could not confirm that Kestrel's data is up to date. | Close and reopen Kestrel. If it keeps happening, make a backup in Settings before anything else. |
| <a id="chk-022"></a>CHK-022 | **An online model is not set up fully.** The check found an online model choice that is missing a setting or a key. | Open Models and finish setting up the online model, or switch back to this computer. |
| <a id="chk-023"></a>CHK-023 | **The search model is not ready.** The model Kestrel uses to compare roles is not set up or is not available. | Follow the suggestion beside the check, then run it again. |
| <a id="chk-024"></a>CHK-024 | **Your saved settings could not be read.** The check could not read the settings you changed in the app, so it used the defaults. | Close and reopen Kestrel. Make a backup in Settings if it keeps happening. |
| <a id="chk-025"></a>CHK-025 | **Model tests were skipped during a run.** The check skipped its model tests because a run is in progress. | Run the check again when the run has finished. |
| <a id="chk-026"></a>CHK-026 | **A model may be too large for this computer.** The largest model a profile uses may not fit in this computer's memory, so it could run slowly or fail to load. | Follow the suggestion beside the check: pick a smaller model or a lighter profile. |
| <a id="chk-027"></a>CHK-027 | **The check could not run.** The check could not finish, so it does not say whether anything is wrong. | Run the check again. If it keeps happening, use Show details when you ask for help. |
| <a id="chk-028"></a>CHK-028 | **A model has not been built.** Kestrel's version of a model has not been built on this computer yet. | Open Models and choose Fix it, or download and build the missing model. |
| <a id="chk-029"></a>CHK-029 | **Your profile file could not be read.** Kestrel found your profile file but could not read it, so roles are scored against an empty profile. | Open Profile and choose Start fresh. Your old file is kept. |
| <a id="chk-030"></a>CHK-030 | **A browser path you set points at nothing.** A browser location you set does not exist, so Kestrel ignores it and tries the usual browsers. | Fix the browser location in Kestrel's settings, or remove it. |
| <a id="chk-031"></a>CHK-031 | **Your data needs updating.** Kestrel's data is not at the version this app expects. | Close and reopen Kestrel so it can update the data. If it says the data is from a newer version, install the newest Kestrel. |

## Anything else (APP)

| Code | What happened | What to do |
|---|---|---|
| <a id="app-001"></a>APP-001 | **Something went wrong on this page.** Kestrel could not load a page because of a problem it did not expect. | Choose Try again. If it keeps happening, choose Show details and include those details when you ask for help. |

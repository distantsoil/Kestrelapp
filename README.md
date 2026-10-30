<img src="docs/images/kestrel-mark.svg" alt="Kestrel mark" width="96" height="96">

# Kestrel

**A free, calmer way to look for your next job.**

Kestrel is a free, local-first desktop app for any job seeker. It finds openings, explains why they fit, and helps you prepare your resume. You choose where to apply. It runs on Windows, macOS or Linux, with no account to create and no command line needed.

[Download](https://kestrelapp.co/download) · [Guides](https://kestrelapp.co/guides) · [Go Deeper](https://kestrelapp.co/docs) · [Privacy policy](https://kestrelapp.co/privacy) · [Report a problem](https://github.com/distantsoil/Kestrelapp/issues/new/choose)

<a href="https://ko-fi.com/distantsoil"><img src="https://storage.ko-fi.com/cdn/kofi2.png?v=6" height="36" alt="Support Kestrel on Ko-fi"></a>

Kestrel is free. If it helps you, donations keep it going.

![Today](docs/images/walk/today-main-light.png)
![Pipeline](docs/images/walk/pipeline-main-dark.png)
![Role evaluation](docs/images/walk/role-evaluation-light.png)
![Applications](docs/images/walk/applications-main-light.png)

Screenshots use invented data. [See the full screenshot index](docs/images/walk/INDEX.md).

## The promises

- **Free.** No account, no signup, no ads. **You are not the product.**
- **Kestrel never applies, emails or messages anyone for you.** It finds, sorts and prepares. You decide and you send.
- **AI runs on your computer by default.** Or use an online model with your own key: OpenRouter, OpenModel, Anthropic, OpenAI or any OpenAI-compatible service. Kestrel ships with no key of its own.
- **It asks before anything goes to an online model,** and shows what it would send. Scheduled work can use an approval you save.
- **Your working data is stored on your computer.**
- **The source is public** under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

## How it works

1. **Tell it what you want.** Add the titles, pay and work styles you want. You can change these answers later.
2. **Read the reasons, then decide.** Kestrel scores the roles it finds. Read the fit assessment and check the original posting before you make your call.
3. **Come back to your next steps.** Today brings follow-ups and interviews into view. You record what happened and decide what to do next.

From the first search to the final offer, Kestrel helps you:

- **Review the roles you find.** Pipeline groups them for review.
- **Keep track of applications.** The Applications board records where each one stands and its next action. It never submits an application.
- **Keep your own resume wording.** You save versions of your text, and Kestrel selects from them when it tailors a resume. Your saved wording is the source.
- **See what is working.** Analytics shows outcomes and the number of results behind each rate, and says when it is too early to compare.
- **Prepare.** Company research, stories, prep packs, mock interviews, offer comparison and negotiation planning. An optional consulting and contract track is off until you turn it on.

## Private by default

- Your working data is stored on your computer. Choose local AI to keep model work there too.
- If you connect an online service, review its data disclosure first. Some actions ask for their own confirmation.
- Confidential mode can hide your marked current employer in what Kestrel shows or creates. Your source text and backups keep the original names.
- To find openings, Kestrel reads public job boards and company career pages. It never signs in to a site and does not search LinkedIn.

Read the [privacy policy](https://kestrelapp.co/privacy), see [every call Kestrel makes](https://kestrelapp.co/docs/every-call-kestrel-makes), or read [how we tested privacy](https://kestrelapp.co/docs/how-we-tested-privacy).

## Run it on your computer, online, or both

- **On your computer.** Free AI models run locally. Your resume and search stay on your computer.
- **Online, with your key.** Model work runs on an online service you choose, with nothing to download. Free models can be much slower than paid ones. OpenRouter has free online models, a good fit for older or smaller computers, but free models may use what you send for training, so read the data disclosure first. [How to get an OpenRouter key](https://kestrelapp.co/docs/openrouter-key).
- **Both.** Local models for everyday runs, an online model when you choose.

## Install

Download the installer for your computer from the [download page](https://kestrelapp.co/download) or [Releases](https://github.com/distantsoil/Kestrelapp/releases): `Kestrel-Setup-x.y.z.exe` for Windows 11, `Kestrel-x.y.z.dmg` for Apple silicon Macs (macOS 13 or later), or `Kestrel-x.y.z-x86_64.AppImage` for 64-bit Linux. Open it, then follow the six setup steps. Linux details are in [docs/LINUX.md](docs/LINUX.md).

1. **Welcome.** What Kestrel does and what it will not do.
2. **Thinking.** Choose **On this computer**, **Online model**, or **Both**.
3. **You.** Your first name, and whether you want jobs, consulting and contract work, or both.
4. **Resume.** Add your resume text, or skip it and add it later.
5. **Wants.** The titles, pay and work styles you want.
6. **Searches.** Review the searches. They begin only when you press **Start searching**.

Kestrel was tested on Windows 11 with an NVIDIA graphics card. Mac and Linux builds have had less testing. To run the AI on your computer, setup helps you install Ollama, a free app. A smaller computer can use a free online model instead; see [computer requirements](https://kestrelapp.co/docs/requirements).

### Unsigned Release Candidate

The Release Candidate installers are not signed yet; signing comes later. Your computer warns you the first time you open Kestrel. You only do this once.

- **Windows:** when SmartScreen warns, choose **More info**, then **Run anyway**.
- **macOS:** right-click Kestrel in Applications, choose **Open**, then choose **Open** again. If macOS only offers Done or Move to Trash, choose Done, open System Settings, then Privacy & Security, and click **Open Anyway** next to the message about Kestrel.

The [first-launch steps](https://kestrelapp.co/download#first-launch) have pictures. This is a release candidate, so expect a few rough edges and keep your own copy of your resume.

## Good to know

- Kestrel does not search LinkedIn, because LinkedIn does not allow automated access. You can paste a LinkedIn posting in by hand.
- Scoring takes time, especially on free models. A first scan of a hundred openings can take an hour or more, and Today shows how far it has got.
- The AI will sometimes get things wrong. Every summary carries a note to check the posting and research the company yourself.

## Learn more

- [Guides](https://kestrelapp.co/guides): step-by-step help for everyday tasks.
- [Go Deeper](https://kestrelapp.co/docs): the full documentation.
- [User guide](docs/GUIDE.md): every page, including online consent, confidential mode, Undo, exports, and backups.
- [What's new](docs/WHATS-NEW.md): the current release.
- [Privacy policy](https://kestrelapp.co/privacy) and [every call Kestrel makes](https://kestrelapp.co/docs/every-call-kestrel-makes).
- [Terms and disclaimers](https://kestrelapp.co/docs/terms-and-disclaimers).
- [For job boards and sites](https://kestrelapp.co/docs/open-call-to-job-boards) (open call), and how to [ask Kestrel to stop reading your site](https://kestrelapp.co/docs/ask-kestrel-to-stop-reading-your-site).
- [Report a problem or suggest an idea](https://github.com/distantsoil/Kestrelapp/issues/new/choose). A screenshot helps most. GitHub is public, so crop or cover your name, company names and job titles first. The [reporting guide](https://kestrelapp.co/docs/report-a-problem) walks through it.
- Developers: [docs/DEVELOPERS.md](docs/DEVELOPERS.md).

## Why I built this

Most AI job search tools assume you already pay for an AI subscription and are comfortable working from a command line. I built Kestrel for everyone else. It is an app first: you install it, open it, and let it do the searching, sorting and tailoring. You review the results and decide where to apply.

Built with Claude, ChatGPT, Qwen, DeepSeek, NVIDIA Nemotron, MiniMax M3, LongCat, Kimi K2.7 and MiMo.

Nine AI models worked as a council, each one critiquing and reviewing the others' work.

## For developers

Build and test instructions are in [docs/DEVELOPERS.md](docs/DEVELOPERS.md). Source checkouts can start with `uv sync --extra dev` and `uv run kestrel setup`.

## Support Kestrel

Kestrel is free to use. If it has helped you, a donation on Ko-fi helps keep it going.

<a href="https://ko-fi.com/distantsoil"><img src="https://storage.ko-fi.com/cdn/kofi2.png?v=6" height="36" alt="Support Kestrel on Ko-fi"></a>

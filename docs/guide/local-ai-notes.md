# Local AI notes: what Kestrel checks about Ollama and its models

These notes go with the **Local AI check** rows on the Models page (**Health check** card). They say what Kestrel looks at on this computer, what it does not, and what you can do about a warning. The check only reads. It sends nothing anywhere, and it never changes Ollama, your firewall or your models. A warning is advice and never stops scoring.

## The rows

- **Text stays on this computer.** The local backend address is this computer. If it points elsewhere, the row says your text goes to that address.
- **Ollama listens only here.** `OLLAMA_HOST` is unset or this computer. Otherwise other devices on your network may reach Ollama.
- **No cloud-hosted models installed.** A model tag ending in `-cloud` sends prompts to Ollama's servers.
- **Ollama's cloud features are off.** Ollama's FAQ says `disable_ollama_cloud` in `~/.ollama/server.json`, or `OLLAMA_NO_CLOUD=1`, turns them off. Kestrel reads that file and its own environment. A setting made only in Ollama's own startup is invisible to Kestrel, so the row says "not seen turned off", which is not a pass.
- **No unexpected connections.** While the model tests run, and for a few seconds after, Kestrel lists the remote addresses Ollama's programs are connected to. This computer is fine. Another computer on your network or a public address is a warning, with the address and port. Ollama's own download and update services are listed apart.
- **Models are the ones Kestrel tested.** Each installed model's digest is compared with the list below. A different digest means the tag has moved since Kestrel tested it. Kestrel's own `kestrel-*` models are built on your machine and are never "unknown".

A row that could not run says **Unverified**. It is never shown as a pass.

## Limits

- Sampling about ten times a second can miss a very short connection.
- Name lookups are not visible.
- Only Ollama's own programs are watched, not the rest of the computer.
- macOS may refuse to show other programs' connections. The row then says unverified.
- It proves nothing about the future, and it is not a security audit.

Under the Online choice the whole check is not needed and is not run.

## Block Ollama from the internet (optional)

Download your models first: this also blocks downloads and Ollama's update check. Kestrel never changes system settings. Copy a line yourself. These lines have not been tested by Kestrel on your system, so read them before you run them.

- Windows (PowerShell as administrator): `New-NetFirewallRule -DisplayName "Block Ollama outbound" -Direction Outbound -Program "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -Action Block`
- Linux, when Ollama runs as the `ollama` user: `sudo iptables -A OUTPUT -m owner --uid-owner ollama ! -o lo -j REJECT`
- macOS: use an application firewall such as LuLu or Little Snitch to deny `ollama` outbound connections.

## Models Kestrel tested

Kestrel records the digest and test date of each model it ships a plan for, in `kestrel/resources/config/model_digests.json`. Open that file for the current digests and dates. Kestrel tested these for context size, thinking turned off, and structured answers. It did not test training data or behavior outside Kestrel's use. Who made each model, its license and where it comes from are on its page in the Ollama library; check them there before relying on them.


### The models and where to read about them

The size, digest and test date below are the values in `model_digests.json`. Kestrel does not list who made each model or its license here, because that is not recorded in Kestrel's files and a guess would be worse than none. For the maker, license and source of each model, see the model's page in the Ollama library (linked in the last column) before you rely on it.

| Model tag | Size | Digest (start) | Tested on | Maker, license and source |
|---|---|---|---|---|
| `qwen3:4b-instruct-2507-q4_K_M` | 2.5 GB | `sha256:0edcdef34593...` | 2026-11-04 | [Ollama library: qwen3](https://ollama.com/library/qwen3) |
| `qwen3:8b` | 5.2 GB | `sha256:500a1f067a9f...` | 2026-11-04 | [Ollama library: qwen3](https://ollama.com/library/qwen3) |
| `qwen3:14b` | 9.3 GB | `sha256:bdbd181c33f2...` | 2026-11-04 | [Ollama library: qwen3](https://ollama.com/library/qwen3) |
| `qwen3:30b-a3b-instruct-2507-q4_K_M` | 19.0 GB | `sha256:19e422b02313...` | 2026-11-04 | [Ollama library: qwen3](https://ollama.com/library/qwen3) |
| `nomic-embed-text:latest` | 0.3 GB | `sha256:0a109f422b47...` | 2026-11-04 | [Ollama library: nomic-embed-text](https://ollama.com/library/nomic-embed-text) |

See also [Models](models.md) and [Troubleshooting](troubleshooting.md).

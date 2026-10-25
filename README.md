# ChromeOnDeviceInfer

Prompt the on-device LLM that Google Chrome downloads — **from Python, on the command line**.

Chrome quietly downloads a multi-gigabyte model (Gemini Nano, stored under
`OptGuideOnDeviceModel/`) and runs it locally for its built-in AI features. This
tool lets you load that model and send it prompts yourself, with no API key, no
cloud calls, and no extra dependencies.

```console
$ chrome-on-device-infer -p "Explain on-device AI in one sentence."
On-device AI refers to AI processing capabilities residing directly on a user's
device, rather than in the cloud, for faster performance and enhanced privacy.
```

Inference happens entirely inside Chrome on your machine. This tool is only a
bridge: it sends prompts and reads the answers back.

---

## Why a bridge (and not a model loader)

You cannot load the downloaded weights in Python directly:

* `weights.bin` is **not** a plain model file. It is a streamed ZIP-style
  container whose entries are `TOKENIZER_MODEL`, `TF_LITE_PREFILL_DECODE`,
  `TF_LITE_PER_LAYER_EMBEDDER`, `TF_LITE_EMBEDDER`, `TF_LITE_VISION_ENCODER`,
  `TF_LITE_VISION_ADAPTER`, and `METADATA` — a **Gemma 3n-style** bundle.
* The payloads are **encrypted/obfuscated**: they carry no TFLite flatbuffer
  magic (`TFL3`), are not zlib/gzip/lz4/zstd, and every sub-model begins with the
  same cipher prefix (`c93aa02167cbabb3a400ece193aa3c10`).
* Only Chrome's own OptimizationGuide runtime can execute them.

So the only way to run that model from Python is to let Chrome run it, and talk
to Chrome. That is what this tool does, using the browser's built-in **Prompt
API** (`LanguageModel`) over the **Chrome DevTools Protocol**.

→ Full internals in **[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)**.

---

## Requirements

* **Google Chrome 138+** (verified on Chrome 156, Linux). Chromium may work.
* The **on-device model must already be installed** in your Chrome profile.
  Check `chrome://on-device-internals` (enable it first via `chrome://chrome-urls`).
  `chrome://components` should show the Optimization Guide manifest.
* **Python 3.9+**. No third-party packages.
* Linux is the tested platform. macOS/Windows should work with the default
  profile path, but the debug-port workaround is Linux-specific.

---

## Install

```bash
# from a clone
pip install -e .

# or run it without installing
PYTHONPATH=src python3 -m chrome_on_device_infer --help
```

This exposes the `chrome-on-device-infer` command. With [pipx](https://pipx.pypa.io):

```bash
pipx install .
```

---

## Usage

> **Quit Chrome before running**, or use `--attach` with a Chrome you started
> yourself. Chrome only accepts a DevTools port on one instance per profile.

One-shot prompt:

```bash
chrome-on-device-infer -p "Name the planet closest to the Sun."
```

Interactive chat (keeps conversation context across turns):

```bash
chrome-on-device-infer
```

```
you> what is a matryoshka?
nano> A matryoshka is a set of wooden dolls of decreasing size placed one inside another...
you>
```

Attach to a Chrome you started yourself:

```bash
google-chrome --remote-debugging-port=9222 \
  --enable-features=AIPromptAPI \
  --user-data-dir="$HOME/.config/google-chrome"

chrome-on-device-infer --attach
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `-p, --prompt TEXT` | — | Run one prompt, print the answer, exit. |
| `-u, --url URL` | local `127.0.0.1` page | Page used as the Prompt API context. |
| `--port N` | `9222` | DevTools port to launch/attach on. |
| `--attach` | off | Attach to an existing Chrome instead of launching. |
| `--user-data-dir DIR` | platform default | Profile holding the model. |
| `--chrome PATH` | auto-detected | Chrome/Chromium binary. |
| `--wait SECONDS` | `60` | How long to wait for the model to become `available`. |
| `--timeout SECONDS` | `900` | How long to wait for a response after prompting. |
| `--keep-open` | off | Leave Chrome running on exit. |
| `--force` | off | Launch even if Chrome looks like it is already running. |
| `--version` | | Print version. |

Status messages go to **stderr**, model output to **stdout**, so you can pipe
the answer:

```bash
chrome-on-device-infer -p "One word: the opposite of hot." 2>/dev/null
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Unexpected error (CDP/socket/generation failure). |
| `2` | Chrome already running, or no Chrome binary found. |
| `3` | The DevTools endpoint never came up. |
| `4` | The on-device model is not usable (not installed / not eligible / no API). |

---

## Use as a library

```python
from chrome_on_device_infer import bridge, chrome
from chrome_on_device_infer.cdp import CDPClient

page = chrome.LocalPageServer()                       # secure-context page
proc = chrome.launch_chrome(chrome.find_chrome(),
                            chrome.default_user_data_dir(), 9222)
try:
    chrome.wait_for_cdp(9222, proc=proc)
    client = CDPClient(chrome.open_page(9222, page.url), timeout=900)
    chrome.prepare_page(client, page.url)             # wait for the API to bind

    bridge.install(client)
    bridge.open_session(client, wait=60)              # raises ModelUnavailable
    answer = bridge.prompt(client, "What is 2 + 2?")
    print(answer)
finally:
    client.close(); page.close(); chrome.terminate_chrome(proc)
```

---

## Troubleshooting

**`availability="downloadable"` / exit code 4** — the model is not installed for
this profile. Launch Chrome normally once and let it finish downloading, or
point `--user-data-dir` at the profile that has it.

**`availability="downloading"`** — Chrome is mid-install. Raise `--wait`, or
watch `chrome://on-device-internals` until it is ready.

**Chrome 156 expects newer models than your `weights.bin`.** Google rotates the
model behind this API (the current manifest ships Gemma 4-era prompt configs),
so an older on-disk `OptGuideOnDeviceModel/<version>/weights.bin` may never be
used. `availability()` tells you the truth.

**"Chrome appears to be using …" (exit 2)** — quit Chrome, or use `--attach`.

**Nothing is generated, no error** — you are almost certainly on an old Chrome
that never opened a debug port (see the default-directory workaround in
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)), or the API was never exposed.
Run with `--attach` and confirm `chrome://on-device-internals` is happy.

---

## How it works (short version)

```
python CLI
  └── starts a throwaway page on http://127.0.0.1:<random>/   (secure context)
  └── launches Chrome with --remote-debugging-port on your profile
        └── DevTools Protocol over a hand-rolled WebSocket client
              └── Runtime.evaluate → window.LanguageModel
                    └── session.promptStreaming(...)          (on-device GPU/CPU)
```

Details, including the three Chrome quirks that make this surprisingly fiddly,
are in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

---

## Verified on

Chrome 156.0.8078.47 · Linux (Intel/Mesa GPU) · model `2025.8.21.1028` (v3Nano)

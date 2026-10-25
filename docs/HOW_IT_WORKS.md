# How ChromeOnDeviceInfer works under the hood

This document explains what Chrome actually downloads, why running it from
Python requires a browser bridge, and every quirk the bridge has to work around.

---

## 1. What Chrome downloads

When Chrome enables its built-in AI, it populates a directory like:

```
~/.config/google-chrome/OptGuideOnDeviceModel/
└── 2025.8.21.1028/          # one dir per model version
    ├── manifest.json                     # {"BaseModelSpec": {"name": "v3Nano", ...}}
    ├── weights.bin                       # 2.86 GB  ← the model
    ├── cache.bin                         # 1.41 GB
    ├── adapter_cache.bin                 # 0 B
    ├── encoder_cache.bin                 # 0 B
    ├── on_device_model_execution_config.pb
    └── _metadata/verified_contents.json
```

`manifest.json` calls this **`v3Nano`**, i.e. Gemini Nano v3.

### 1.1 `weights.bin` is a bundle, not a tensor dump

The file does not start with a flatbuffer. It starts with a 4-byte value
followed by padding, and at offset **83** you find a ZIP local file header
(`PK\x03\x04`). There is **no** end-of-central-directory record, i.e. it is a
*streamed* archive. Walking the local headers yields these entries:

| Entry | Size | Role |
| --- | --- | --- |
| `TOKENIZER_MODEL` | 4.7 MB | sentencepiece tokenizer |
| `TF_LITE_PREFILL_DECODE` | 1.41 GB | the main transformer |
| `TF_LITE_PER_LAYER_EMBEDDER` | 1.01 GB | per-layer embeddings |
| `TF_LITE_EMBEDDER` | 272 MB | token embeddings |
| `TF_LITE_VISION_ENCODER` | 150 MB | vision tower |
| `TF_LITE_VISION_ADAPTER` | 17.8 MB | vision projector |
| `METADATA` | 173 B | bundle metadata |

That entry set — a per-layer embedder plus a vision encoder/adapter — is the
**Gemma 3n architecture**. The sub-models are LiteRT (TFLite) graphs.

### 1.2 The payloads are encrypted

You cannot simply unzip the entries and hand them to a TFLite interpreter:

* No `TFL3` flatbuffer identifier anywhere in the payload headers.
* Not zlib, raw-deflate, gzip, lz4, or zstd — every decompressor fails.
* Every sub-model payload starts with the **same 16-byte prefix**:

  ```
  VISION_ENCODER      c93aa021 67cbabb3 a400ece1 93aa3c10
  VISION_ADAPTER      c93aa021 67cbabb3 a400ece1 93aa3c10
  PER_LAYER_EMBEDDER  c93aa021 67cbabb3 a400ece1 93aa3c10
  EMBEDDER            c93aa021 67cbabb3 a400ece1 93aa3c10
  PREFILL_DECODE      f53aa021 67cbabb3 b000cce1 83aa1410
  ```

  Identical ciphertext prefixes across different models mean an identical
  keystream over similar plaintext headers — i.e. a deterministic
  encryption/obfuscation layer, not compression. `TOKENIZER_MODEL` and
  `METADATA` are similarly opaque.

So the practical conclusion: **the bytes are not usable outside Chrome's own
OptimizationGuide model executor.** Even without the encryption, you would need
a LiteRT runtime that supports the exact operator set and the Gemma 3n
per-layer-embedding execution graph.

---

## 2. The only door: Chrome's Prompt API

Chrome exposes the loaded model to web content as the **Prompt API**:

```js
const availability = await LanguageModel.availability(); // "available" | ...
const session = await LanguageModel.create();
const text = await session.prompt("hello");
// or stream:
for await (const chunk of session.promptStreaming("hello")) { ... }
```

Requirements that shape this tool:

* It is available to **top-level windows** in a **secure context**.
* Chrome ships it to web pages from Chrome 148 (extensions from 138).
* On-device model availability is Chrome's decision, not ours (§4.3).

Since the model already lives in the browser, the tool's job is reduced to:
**open a page, evaluate JavaScript in it, and read values back.** That is the
DevTools Protocol.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ chrome-on-device-infer (Python, stdlib only)                         │
│                                                                      │
│  cli.py        argparse wiring, exit codes, REPL                     │
│  chrome.py     binary discovery, launch/terminate, XDG workaround,   │
│                DevTools HTTP endpoints, local page server            │
│  cdp.py        hand-rolled WebSocket + CDP command layer             │
│  bridge.py     window.__ai helper JS, availability, session, prompt  │
└───────────────┬──────────────────────────────────────────────────────┘
                │  HTTP  GET /json/version, PUT /json/new?<url>
                │  WebSocket   ws://127.0.0.1:9222/devtools/page/<id>
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Google Chrome (your profile, model already downloaded)               │
│                                                                      │
│  Renderer page:  http://127.0.0.1:<random>/   ← served by us         │
│      └── window.LanguageModel  (Prompt API)                          │
│              └── on-device model  (GPU/CPU, fully local)             │
└──────────────────────────────────────────────────────────────────────┘
```

No third-party Python packages: the WebSocket client is ~120 lines of RFC 6455
(masked text frames out, length-prefixed frames in, ping/pong handling).

---

## 4. Lifecycle, and the quirks that bite

### 4.1 Launch, and Chrome's refusal to debug the default profile

Chrome 136+ **refuses to open a remote-debugging port when the data directory is
the default one**:

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

This is why naive automation silently produces *nothing*: Chrome starts, but no
debug port exists, so the CDP connection never happens. Both
`--remote-debugging-port` and `--remote-debugging-pipe` are affected.

We must use the profile that contains the model, which *is* the default profile.
The escape hatch: the check compares against Chrome's **computed default**,
which on Linux is `$XDG_CONFIG_HOME/google-chrome`. `chrome.py` therefore:

1. creates a temp dir (e.g. `/tmp/chrome-cdp-xdg-<pid>/`),
2. symlinks every entry of `~/.config` into it (so fontconfig and friends still
   resolve) **except `google-chrome`**,
3. launches with `XDG_CONFIG_HOME=<temp>` and an explicit
   `--user-data-dir=$HOME/.config/google-chrome`.

Chrome's default is now `<temp>/google-chrome` (which does not exist), our data
directory is the real profile, the two differ, and the port opens. The temp dir
is removed on exit.

> Note: pointing `--user-data-dir` at a *symlink* of the profile does **not**
> work — Chrome canonicalises the path before comparing.

### 4.2 A page target, and the opaque-origin race

`PUT /json/new?<url>` returns a page target plus its `webSocketDebuggerUrl`. The
target starts on `about:blank`, which has an **opaque origin**; the
`LanguageModel` binding is not installed there.

Evaluating immediately therefore reports the API as *missing*. `prepare_page()`
navigates and then polls until either `typeof globalThis.LanguageModel ===
'function'` or the document has finished loading, which is why the tool uses a
**real HTTP origin** — and specifically `http://127.0.0.1:<port>/`, because
loopback is a *secure context* and needs no network access. `data:` URLs and
`about:blank` do not qualify.

### 4.3 Availability is a state machine, and `create()` can hang

`LanguageModel.availability()` returns:

| Value | Meaning | What the tool does |
| --- | --- | --- |
| `available` | Ready. | Proceed. |
| `downloading` | Chrome is installing the model. | Poll up to `--wait`. |
| `downloadable` | Not installed for this profile. | Report and exit `4`. |
| `unavailable` | Device/profile ineligible. | Report and exit `4`. |

In the non-ready states, `LanguageModel.create()` does **not** fail fast — it
blocks, and can hang indefinitely with no download progress. That is exactly the
"no response, no error" symptom. Hence the tool gates on `availability()` first
and never calls `create()` unless the model is `available`.

Worth knowing: Chrome rotates the model behind this API. The OptimizationGuide
manifest observed at the time of writing (`1.20261018.2`) ships
`prompt_gemma4_*_solution_config.binarypb` and Chrome queries
`optimizationguide-pa.googleapis.com/v1:GetModels` to decide what to fetch — so
an older `weights.bin` on disk may simply be superseded.

### 4.4 Streaming without events

`Runtime.evaluate` is request/response, and `promptStreaming()` returns a
`ReadableStream` that cannot cross the CDP boundary directly. Instead the page
helper starts the stream and accumulates text on `window.__ai.out`:

```js
window.__ai.ask = (text) => {
  window.__ai.out = ''; window.__ai.done = false;
  (async () => {
    const stream = window.__ai.session.promptStreaming(text);
    for await (const chunk of stream) {
      // Chrome has shipped both incremental and cumulative chunking.
      window.__ai.out = chunk.startsWith(window.__ai.out)
        ? chunk : window.__ai.out + chunk;
    }
    window.__ai.done = true;
  })();
  return true;
};
```

Python polls `window.__ai.state()` a few times a second and emits the delta,
which produces live token output. The `startsWith` check makes the accumulation
correct under either chunking convention.

### 4.5 Killing Chrome reliably

Chrome **re-execs** itself during startup, so the PID returned by
`subprocess.Popen` is frequently not the process that survives.
`proc.terminate()` therefore leaves Chrome running. The launcher starts Chrome
in its own session (`start_new_session=True`) and teardown signals the whole
**process group** (`os.killpg`), escalating `SIGTERM` → `SIGKILL`.

### 4.6 Stale profile locks

Chrome writes `SingletonLock` → `<hostname>-<pid>`. The lock survives crashes and
clean quits, so testing for the file alone produces a false "Chrome is running".
The tool parses the PID out of the symlink and checks it with `os.kill(pid, 0)`.

### 4.7 The WebSocket handshake

Chrome rejects handshakes whose `Origin` header is not listed in
`--remote-allow-origins`, but exempts non-browser clients that omit `Origin`
entirely. `cdp.py` deliberately sends no `Origin`, and the launch still passes
`--remote-allow-origins=*` so browser-ish clients work too.

---

## 5. Module map

| Module | Responsibility |
| --- | --- |
| `cdp.py` | RFC 6455 WebSocket client; `CDPClient.call/evaluate`. Ignores protocol events. |
| `chrome.py` | Locate/launch/terminate Chrome, the XDG workaround, `/json/*` endpoints, `prepare_page`, `LocalPageServer`. |
| `bridge.py` | `window.__ai` helper JS, `resolve_availability`, `open_session`, `prompt`, `destroy_session`, `ModelUnavailable`. |
| `cli.py` | Argument parsing, streaming to stdout, exit codes, REPL. |

---

## 6. Limitations / non-goals

* **Requires Chrome to be installed and the model to be present.** This is a
  bridge, not a runtime; it does not download the model into Python.
* **One Chrome per profile.** Chrome allows a DevTools port on only one instance
  per profile, so the tool refuses to start when Chrome is already running
  (`--force` / `--attach` override).
* **Model-dependent output.** Sampling parameters are not exposed to web pages
  by default (they are gated behind `samplingMode` and an origin trial), so the
  tool cannot set temperature/top-k on the web.
* **Linux-first.** The XDG workaround is Linux-specific; on other platforms the
  default-directory restriction may or may not apply.
* **Not affiliated with Google.** You are using Chrome's own on-device model
  through Chrome's own API; Google's generative-AI terms apply to the model.

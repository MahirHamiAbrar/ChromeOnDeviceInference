"""The model-facing half: availability, sessions, and prompting.

Everything here drives ``window.LanguageModel`` inside the page.  Because
``Runtime.evaluate`` is request/response, generation is done by kicking off a
streaming prompt in the page and *polling* the page for the text accumulated so
far, which is what makes token-by-token output possible over CDP.
"""

from __future__ import annotations

import json
import time

from .cdp import CDPClient

# Page-side helper.  Kept as one string so a single evaluate installs it.
# A live session is stored on window.__ai so multi-turn prompts keep context.
INSTALL_JS = r"""
(() => {
  window.__ai = window.__ai || {};
  window.__ai.LM = globalThis.LanguageModel
      || (globalThis.ai && globalThis.ai.languageModel) || null;

  window.__ai.init = async () => {
    const LM = window.__ai.LM;
    if (!LM) {
      return {
        ok: false,
        error: 'LanguageModel is not exposed on this page',
        hint: 'Enable chrome://flags/#prompt-api-for-gemini-nano, or launch with '
            + '--enable-features=AIPromptAPI.',
      };
    }
    let availability;
    try {
      availability = await LM.availability();
    } catch (e) {
      return { ok: false, error: 'LanguageModel.availability() threw: ' + e };
    }
    if (window.__ai.session) return { ok: true, availability, reused: true };
    if (availability !== 'available') {
      return {
        ok: false,
        availability,
        error: 'model is not ready (availability="' + availability + '")',
      };
    }
    window.__ai.session = await LM.create();
    return { ok: true, availability };
  };

  window.__ai.ask = (text) => {
    window.__ai.out = '';
    window.__ai.done = false;
    window.__ai.err = null;
    (async () => {
      try {
        const stream = window.__ai.session.promptStreaming(text);
        for await (const chunk of stream) {
          // Chrome has shipped both incremental and cumulative chunking; this
          // accepts either.
          window.__ai.out = chunk.startsWith(window.__ai.out)
              ? chunk
              : window.__ai.out + chunk;
        }
      } catch (e) {
        window.__ai.err = String(e);
      } finally {
        window.__ai.done = true;
      }
    })();
    return true;
  };

  window.__ai.state = () => ({
    out: window.__ai.out,
    done: window.__ai.done,
    err: window.__ai.err,
  });

  window.__ai.destroy = () => {
    if (window.__ai.session) {
      window.__ai.session.destroy();
      window.__ai.session = null;
    }
    return true;
  };

  return { hasLanguageModel: !!window.__ai.LM };
})()
"""

# availability() states, per the Prompt API.
_TERMINAL_STATES = ("available", "unavailable", "no-api")

# Longer explanatory text for the states where Chrome, not us, is the blocker.
_STATE_HELP = {
    "downloading": (
        "Chrome is still installing the on-device model. Let Chrome finish "
        "(watch chrome://on-device-internals), then retry."
    ),
    "downloadable": (
        "Chrome has not installed the on-device model for this profile yet. "
        "Open Chrome normally and let it download the model, then retry."
    ),
    "unavailable": (
        "This device/profile is not eligible for the on-device model, or on-device "
        "AI is disabled. Check chrome://on-device-internals and Chrome's settings."
    ),
    "no-api": (
        "The Prompt API is not exposed here. Enable "
        "chrome://flags/#prompt-api-for-gemini-nano, or launch with "
        "--enable-features=AIPromptAPI."
    ),
}


class ModelUnavailable(RuntimeError):
    """The on-device model cannot be prompted right now."""


def install(client: CDPClient) -> dict:
    """Install the page-side helper and report whether an API was found."""
    info = client.evaluate(INSTALL_JS, user_gesture=True)
    if not isinstance(info, dict):
        raise ModelUnavailable("could not install the page-side helper")
    return info


def resolve_availability(
    client: CDPClient, wait: float, log=lambda message: None
) -> str:
    """Poll ``availability()`` until it settles, or ``wait`` seconds elapse.

    ``downloading``/``downloadable`` are not terminal - Chrome may still be
    installing the model - so we give it a chance to become ``available``
    instead of blocking forever on ``create()``.
    """
    deadline = time.time() + wait
    last = None
    while True:
        state = client.evaluate(
            "globalThis.LanguageModel"
            " ? globalThis.LanguageModel.availability() : 'no-api'"
        )
        if state != last:
            log(f"model availability: {state}")
            last = state
        if state in _TERMINAL_STATES:
            return state
        if time.time() > deadline:
            return state
        time.sleep(2)


def open_session(client: CDPClient, wait: float, log=lambda message: None) -> None:
    """Wait for the model, then create a session.  Raises ModelUnavailable."""
    availability = resolve_availability(client, wait, log=log)
    if availability != "available":
        raise ModelUnavailable(
            f'availability="{availability}": '
            + _STATE_HELP.get(availability, "model is not usable")
        )

    status = client.evaluate("window.__ai.init()", user_gesture=True) or {}
    if not status.get("ok"):
        raise ModelUnavailable(status.get("error", "could not create a session"))


def prompt(
    client: CDPClient,
    text: str,
    on_chunk=None,
    poll: float = 0.4,
    timeout: float = 900.0,
) -> str:
    """Send a prompt and stream the answer back; returns the full text.

    The answer is generated inside the page; here we poll the accumulated text
    and hand the delta to ``on_chunk``.
    """
    client.evaluate(f"window.__ai.ask({json.dumps(text)})")

    printed = 0
    deadline = time.time() + timeout
    while True:
        state = client.evaluate("window.__ai.state()") or {}
        output = state.get("out") or ""
        if len(output) > printed:
            if on_chunk is not None:
                on_chunk(output[printed:])
            printed = len(output)
        if state.get("done"):
            if state.get("err"):
                raise RuntimeError(state["err"])
            return output
        if time.time() > deadline:
            raise TimeoutError("the model did not finish responding in time")
        time.sleep(poll)


def destroy_session(client: CDPClient) -> None:
    try:
        client.evaluate("window.__ai.destroy()")
    except Exception:
        pass

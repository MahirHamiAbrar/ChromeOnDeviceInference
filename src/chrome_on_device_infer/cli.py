"""Command-line entry point: ``chrome-on-device-infer``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import bridge, chrome
from .cdp import CDPClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chrome-on-device-infer",
        description=(
            "Load and prompt the on-device LLM that Google Chrome downloads "
            "(Gemini Nano / OptGuideOnDeviceModel), using the browser's built-in "
            "Prompt API over the DevTools Protocol."
        ),
        epilog=(
            "Quit Chrome before running, or use --attach with a Chrome you "
            "started yourself. See docs/HOW_IT_WORKS.md for the internals."
        ),
    )
    parser.add_argument(
        "-p", "--prompt",
        help="run a single prompt, print the answer, and exit",
    )
    parser.add_argument(
        "-u", "--url",
        help="page to run the Prompt API in (default: a local 127.0.0.1 page)",
    )
    parser.add_argument(
        "--port", type=int, default=9222,
        help="DevTools port to launch/attach on (default: 9222)",
    )
    parser.add_argument(
        "--attach", action="store_true",
        help="attach to an already-running Chrome instead of launching one",
    )
    parser.add_argument(
        "--user-data-dir", default=chrome.default_user_data_dir(),
        help="Chrome profile directory holding the downloaded model",
    )
    parser.add_argument(
        "--chrome",
        help="path to the Chrome/Chromium binary",
    )
    parser.add_argument(
        "--wait", type=float, default=60.0,
        help="seconds to wait for the model to become available (default: 60)",
    )
    parser.add_argument(
        "--timeout", type=float, default=900.0,
        help="seconds to wait for a response after prompting (default: 900)",
    )
    parser.add_argument(
        "--keep-open", action="store_true",
        help="leave Chrome running after the script exits",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="launch even if a Chrome instance seems to be running already",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser


def _emit(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _status(message: str) -> None:
    print(f"[*] {message}", file=sys.stderr)


def _fail(message: str, code: int = 1) -> int:
    print(f"[!] {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    page_server = None
    if args.url:
        page_url = args.url
    else:
        page_server = chrome.LocalPageServer()
        page_url = page_server.url

    proc = None
    client = None
    try:
        if not args.attach:
            if chrome.chrome_is_running(args.user_data_dir) and not args.force:
                return _fail(
                    f"Chrome appears to be using {args.user_data_dir}.\n"
                    "    Quit Chrome first, or re-run with --attach / --force.",
                    code=2,
                )
            binary = args.chrome or chrome.find_chrome()
            if not binary:
                return _fail(
                    "Could not find a Chrome/Chromium binary; pass --chrome.", code=2
                )
            _status(f"launching Chrome (profile: {args.user_data_dir})")
            proc = chrome.launch_chrome(binary, args.user_data_dir, args.port)

        if not chrome.wait_for_cdp(args.port, timeout=45, proc=proc):
            return _fail(
                f"the DevTools endpoint never came up on port {args.port}", code=3
            )

        client = CDPClient(chrome.open_page(args.port, page_url), timeout=args.timeout)
        chrome.prepare_page(client, page_url)
        bridge.install(client)
        bridge.open_session(client, args.wait, log=_status)
        _status("session ready")

        if args.prompt:
            _status("thinking")
            bridge.prompt(client, args.prompt, on_chunk=_emit, timeout=args.timeout)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            _status("interactive mode - type a prompt, Ctrl-D to quit")
            while True:
                try:
                    text = input("you> ")
                except EOFError:
                    break
                if not text.strip():
                    continue
                sys.stdout.write("nano> ")
                sys.stdout.flush()
                bridge.prompt(client, text, on_chunk=_emit, timeout=args.timeout)
                sys.stdout.write("\n")
                sys.stdout.flush()

        bridge.destroy_session(client)
    except KeyboardInterrupt:
        return 1
    except bridge.ModelUnavailable as exc:
        return _fail(str(exc), code=4)
    except Exception as exc:  # noqa: BLE001 - surface any CDP/socket failure
        return _fail(str(exc), code=1)
    finally:
        if client is not None:
            client.close()
        if page_server is not None:
            page_server.close()
        chrome.cleanup_xdg_config()
        if proc is not None and not args.keep_open:
            chrome.terminate_chrome(proc)
            _status("Chrome closed")

    return 0

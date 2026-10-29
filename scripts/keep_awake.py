#!/usr/bin/env python3
"""Keep the public Streamlit dashboard awake, and prove it actually rendered.

WHY A BROWSER AND NOT curl
--------------------------
Streamlit Community Cloud decides an app is idle from *viewer sessions*, which exist only
once a client opens the websocket at ``/_stcore/stream`` and the server creates a session
for it. A plain HTTP GET fetches the page shell and nothing else, so it never registers as
a visitor and never resets the idle timer.

That was measured, not assumed:

* ``curl`` of ``/~/+/`` returned **HTTP 502** while the app was asleep, yet opening the same
  URL in a real browser woke it and rendered the dashboard.
* ``/healthz`` answers ``200 {"status":"ok"}`` even then, because it is served by the Cloud
  edge rather than the app container. It is useless as a liveness signal for the app.
* A bare websocket to ``/~/+/_stcore/stream`` completes the handshake but receives no
  ``ForwardMsg``, so no session is created. Hand-rolling the protocol would be fragile and
  would break whenever Streamlit changes it.

So this drives a real browser. Whatever Streamlit counts as a viewer, a browser is one.

It also verifies that content only present *after* a session renders is on the page, so a
green run means the dashboard really served - not merely that an edge returned 200.
"""

from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://iris-signal-harvester.streamlit.app/"

# Text that exists only once Streamlit has opened a session and rendered the app. The page
# shell alone never contains it, which is what makes this a real check.
RENDERED_MARKERS = ("TOTAL SIGNALS", "IRIS Signal Harvester")

# Community Cloud shows a "Yes, get this app back up!" button when an app has been put to
# sleep. If it appears, click it and keep waiting.
WAKE_BUTTON = "button:has-text('get this app back up')"


def _wait_for_markers(page, markers: tuple[str, ...], timeout_ms: int) -> None:
    """Wait for every marker, looking in the main frame and in any iframe.

    Raises ``PWTimeout`` if a marker never appears anywhere, so the caller's retry and
    non-zero exit still work.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    for marker in markers:
        while True:
            for frame in page.frames:
                try:
                    if frame.locator(f"text={marker}").count():
                        where = (
                            "main frame"
                            if frame is page.main_frame
                            else f"iframe {frame.url[-24:]}"
                        )
                        print(f"  rendered: found {marker!r} in the {where}", flush=True)
                        break
                except Exception:  # a frame can detach mid-poll; just try the next one
                    continue
            else:
                if time.monotonic() > deadline:
                    raise PWTimeout(f"marker {marker!r} did not appear in any frame")
                page.wait_for_timeout(1_000)
                continue
            break


def keep_awake(url: str, hold_seconds: float, attempts: int, verbose: bool) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            for attempt in range(1, attempts + 1):
                print(f"attempt {attempt}/{attempts}: opening {url}", flush=True)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                    status = response.status if response else "none"
                    print(f"  navigation status: {status}", flush=True)

                    # A sleeping app shows a wake button instead of the dashboard. It lives
                    # in the outer page, so look for it before descending into the frame.
                    if page.locator(WAKE_BUTTON).count():
                        print("  app was asleep; clicking the wake button", flush=True)
                        page.locator(WAKE_BUTTON).first.click()

                    # Community Cloud serves the app inside an iframe (/~/+/), so the
                    # dashboard's own text is never in the main frame. Searching only the
                    # main frame times out even when the app has rendered perfectly.
                    _wait_for_markers(page, RENDERED_MARKERS, timeout_ms=120_000)

                    # Hold the session open so it is unambiguously an active viewer rather
                    # than a connect-and-drop.
                    print(f"  holding the session for {hold_seconds:.0f}s", flush=True)
                    time.sleep(hold_seconds)

                    title = page.title()
                    print(f"  done; page title: {title!r}", flush=True)
                    return 0

                except PWTimeout as exc:
                    print(f"  timed out: {str(exc).splitlines()[0]}", flush=True)
                    if verbose:
                        body = (page.content() or "")[:400].replace("\n", " ")
                        print(f"  page began: {body}", flush=True)
                    if attempt < attempts:
                        backoff = attempt * 20
                        print(f"  waiting {backoff}s before retrying", flush=True)
                        time.sleep(backoff)

            print(f"ERROR: {url} never rendered after {attempts} attempts", file=sys.stderr)
            return 1
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=45.0,
        help="how long to stay connected once rendered (default: 45)",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return keep_awake(args.url, args.hold_seconds, args.attempts, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

"""E2E: Android server selection must not obscure the header's Web/TUI toggle.

The Android shell (``web/android``) floats a native server-switcher pill
top-center over the WebView (``switchButton`` in ``MainActivity.kt``):
``Gravity.TOP | CENTER_HORIZONTAL``, ``topMargin = safeTop + 8dp``, 12sp text
with 12dp/6dp padding, WRAP_CONTENT width with no cap, elevated above the page,
and always visible. The SPA's mobile chat header floats its own control
clusters in the same top band (``.chat-header`` at ``max(0, safeTop - 0.5rem)``,
44px-tall pills), with the Chat/Terminal ("Web/TUI") switcher
(``ViewModeToggle``) in the trailing cluster on terminal-first sessions. On a
phone-width screen a real-world server hostname makes the centered pill wide
enough to reach the trailing cluster, so the native pill sits across the
Web/TUI toggle and obscures both controls.

The iOS shell had this exact defect and fixed it by consolidating server
selection into the web sidebar picker and never floating the pill over the
chat surface (``web/src/shell/SidebarServerPicker.tsx``,
``web/src/hooks/useNativeServerSwitcher.ts``); the placement contract is
pinned by ``test_ios_server_selector_placement.py``. Android never got that
treatment: the web half of the contract is gated to ``isIOSShell()`` and the
Android bridge (``NativeBridgeScript.kt``) exposes neither
``setServerSwitcherHidden`` nor ``getServerPicker``, so the pill floats over
the chat header unconditionally.

The pill itself is native Android chrome that Playwright cannot render, but
its placement is a web-visible contract, and this test drives the SPA the way
a picker-capable Android WebView shell does — phone viewport, an injected
``window.omnigentNative`` bridge with ``kind: "android"`` carrying the same
hide/picker surface the fixed iOS shell provides, and the OS status-bar inset
pushed the way ``MainActivity.emitInsets`` pushes it. It then asserts the same
two halves the iOS placement test pins:

* while the chat surface is frontmost the web must ask the shell to keep the
  floating switcher hidden (``setServerSwitcherHidden(true)``), and
* the pill's floating band — computed from the shell's own layout rules and
  the SPA's published footprint vars (``--omnigent-android-switcher-*``) —
  must not sit across the header's Web/TUI toggle (or the header band at all)
  unless the web requested it hidden.

Before a fix the web never drives the Android bridge's hide affordance, the
pill stays visible, and its band lands across the Web/TUI toggle — the
reported overlap.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, Route, ViewportSize, expect

# Phone-sized viewport: the Android shell is a phone surface, and the narrow
# width is where the centered pill reaches the header's trailing cluster.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# OS status-bar inset of a typical Android phone, CSS px. Android WebView
# reports env(safe-area-inset-*) as 0, so the shell injects the OS-measured
# inset inline (MainActivity.emitInsets); the test does the same below.
_SAFE_TOP_PX = 24.0

# The native pill's own layout constants, mirroring MainActivity.kt's
# switchButton: 12sp text (== 12 CSS px at fontScale 1) and 12dp horizontal
# padding per side. Vertical placement/height come from the SPA's published
# footprint vars (--omnigent-android-switcher-margin/-height in index.css),
# read live so the test can't drift from the layout's own model of the pill.
_SWITCHER_TEXT_PX = 12.0
_SWITCHER_H_PADDING_PX = 24.0

# Host label the pill renders (hostLabelOf in MainActivity.kt: host[:port] of
# the connected server). A hosted-deployment hostname of the class the report
# hit — long enough that the uncapped, centered pill spans most of a phone
# width. iOS caps its pill at min(172, 0.38 * width); Android has no cap.
_HOSTED_SERVER_HOST = "omnigent-workspace-3272836215.aws.example-apps.com"

# Another server the shell remembers, offered through the picker payload —
# the same shape the iOS placement test feeds getServerPicker.
_ALT_SERVER_URL = "https://alt-server.example.test:8443/"
_ALT_SERVER_HOST = "alt-server.example.test:8443"

# Stand-in for the Android WebView bridge of a shell that carries the same
# server-selection surface the fixed iOS shell provides (hide affordance +
# sidebar-picker payload). Runs before any app script (add_init_script) so
# nativeApi() in nativeBridge.ts sees an Android shell: `kind` drives
# isAndroidShell(), setServerSwitcherHidden records every hide/show request so
# the test can assert what the web told the shell, the server-picker trio
# mirrors the payload the iOS shell pushes, and the rest keep unrelated native
# calls (theme / badge / notify / insets / view mode) from throwing under the
# stub. An old shell without these affordances can't be steered by the web at
# all — its pill is unconditionally visible — so the fixable contract is what
# this stub represents.
_ANDROID_SHELL_INIT_SCRIPT = f"""
window.__switcherHiddenCalls = [];
window.__switchServerCalls = [];
window.omnigentNative = {{
  kind: "android",
  setColorScheme: function () {{}},
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  onNativeInsets: function () {{ return function () {{}}; }},
  setViewMode: function () {{}},
  onViewModeChanged: function () {{ return function () {{}}; }},
  setServerSwitcherHidden: function (hidden) {{
    window.__switcherHiddenCalls.push(hidden);
  }},
  getServerPicker: function () {{
    return Promise.resolve({{
      currentOrigin: location.origin,
      managedServers: [],
      recentServers: [location.origin + "/", "{_ALT_SERVER_URL}"],
    }});
  }},
  switchServer: function (url) {{
    window.__switchServerCalls.push(url);
    return Promise.resolve();
  }},
  openServerSetup: function () {{}},
}};
"""

# Compute the native pill's floating band, in CSS px, from the shell's layout
# rules: horizontally centered, WRAP_CONTENT (label width at 12px + 24px
# padding, uncapped — clamped only by the screen), floating
# --omnigent-android-switcher-margin below the OS inset at
# --omnigent-android-switcher-height tall (the footprint the SPA itself
# publishes for the pill in index.css and uses for its scroll fade).
_SWITCHER_BAND_JS = """
([label, safeTop, textPx, hPadPx]) => {
  const root = getComputedStyle(document.documentElement);
  const margin = parseFloat(root.getPropertyValue('--omnigent-android-switcher-margin')) || 8;
  const height = parseFloat(root.getPropertyValue('--omnigent-android-switcher-height')) || 25;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  ctx.font = `${textPx}px Roboto, "Helvetica Neue", Arial, sans-serif`;
  const width = Math.min(window.innerWidth, ctx.measureText(label).width + hPadPx);
  const left = (window.innerWidth - width) / 2;
  const top = safeTop + margin;
  return { left: left, top: top, right: left + width, bottom: top + height };
}
"""


def _mark_terminal_first(base_url: str, session_id: str) -> None:
    """Stamp the session terminal-first (``omnigent.ui = terminal``).

    The Chat/Terminal switcher only exists for terminal-first sessions, so the
    label is the journey's precondition — the same one ``omnigent claude`` /
    ``omnigent codex`` sessions carry.

    :param base_url: Spawned server base URL.
    :param session_id: Session to label.
    """
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _route_agent_terminal(page: Page, session_id: str) -> None:
    """Serve a deterministic agent terminal pane for the session.

    The switcher's placement does not need a live PTY, only the resource shape
    the runner publishes — mirrors ``test_ios_switcher_in_header.py``.

    :param page: Page whose network to intercept.
    :param session_id: Session whose terminals list to stub.
    """

    def _serve(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_tui_main",
                            "type": "terminal",
                            "session_id": session_id,
                            "name": "tui:main",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": "terminal_tui_main",
                    "last_id": "terminal_tui_main",
                    "has_more": False,
                }
            ),
        )

    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve)


def _as_rect(box: dict[str, float]) -> dict[str, float]:
    """Convert a Playwright bounding box to a left/top/right/bottom rect.

    :param box: ``{"x", "y", "width", "height"}`` from ``bounding_box()``.
    :returns: The rect as ``{"left", "top", "right", "bottom"}``.
    """
    return {
        "left": box["x"],
        "top": box["y"],
        "right": box["x"] + box["width"],
        "bottom": box["y"] + box["height"],
    }


def _intersects(a: dict[str, float], b: dict[str, float]) -> bool:
    """Whether two left/top/right/bottom rects overlap.

    :param a: First rect.
    :param b: Second rect.
    :returns: True when the rects share any area.
    """
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def _open_android_terminal_first_session(page: Page, seeded_session: tuple[str, str]) -> None:
    """Drive the reported journey: open a terminal-first session in the Android app.

    Phone viewport, the injected Android bridge, then the session at
    ``/c/<id>`` with the OS status-bar inset pushed inline the way
    ``MainActivity.emitInsets`` pushes it. Waits for the composer so the
    assertions that follow fail on the bug, not on hydration timing.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    _mark_terminal_first(base_url, session_id)
    _route_agent_terminal(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # Gate: the SPA recognized the Android shell (isAndroidShell() -> AppShell tag).
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")

    # Inject the status bar's OS inset exactly as the shell does: inline on
    # the document root, into both the shared fold var and the Android var
    # (MainActivity.emitInsets sets both; Chromium can't emulate env()).
    page.evaluate(
        "() => {"
        " const s = document.documentElement.style;"
        f" s.setProperty('--omnigent-safe-top', '{_SAFE_TOP_PX}px');"
        f" s.setProperty('--omnigent-android-safe-area-top', '{_SAFE_TOP_PX}px');"
        "}"
    )

    composer = page.locator('textarea[aria-label="Message the agent"]')
    expect(composer).to_be_visible(timeout=60_000)


def test_android_server_switcher_stays_clear_of_web_tui_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Android shell's server switcher must not obscure the Web/TUI toggle.

    Drives the reported journey: connect the Android app to a server with a
    real-world hostname, open a terminal-first session (whose header carries
    the Chat/Terminal "Web/TUI" toggle) on a phone-width screen, and check
    where the native server-switcher pill floats. The web must ask a
    picker-capable shell to keep the pill hidden over the chat surface (the
    iOS treatment), and the pill's band must never sit across the header's
    Web/TUI toggle. Before a fix the web never drives the Android bridge's
    hide affordance, the always-visible pill floats inside the chat-header
    band, and a real-world host label makes it wide enough to extend across
    the toggle — obscuring both controls.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    _open_android_terminal_first_session(page, seeded_session)

    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=8_000)
    header = page.locator(".chat-header")
    expect(header).to_be_visible()

    # Let the mount-time bridge pushes and layout settle before measuring.
    page.wait_for_timeout(500)

    # The shell shows the pill unless the web's latest request hid it. The
    # real shell today has no hide affordance at all and the web never asks —
    # both make the pill visible over the chat surface.
    hidden_calls = page.evaluate("() => window.__switcherHiddenCalls")
    switcher_shown = not hidden_calls or not hidden_calls[-1]

    pill = page.evaluate(
        _SWITCHER_BAND_JS,
        [_HOSTED_SERVER_HOST, _SAFE_TOP_PX, _SWITCHER_TEXT_PX, _SWITCHER_H_PADDING_PX],
    )
    toggle_box = toggle.bounding_box()
    header_box = header.bounding_box()
    assert toggle_box is not None and header_box is not None

    print(f"[android-switcher] setServerSwitcherHidden calls: {hidden_calls}")
    print(f"[android-switcher] pill band: {pill}")
    print(f"[android-switcher] view-mode-toggle: {_as_rect(toggle_box)}")
    print(f"[android-switcher] chat-header: {_as_rect(header_box)}")

    violations: list[str] = []
    if switcher_shown and _intersects(pill, _as_rect(toggle_box)):
        violations.append(
            f"the server-switcher band {pill} extends across the Web/TUI toggle "
            f"{_as_rect(toggle_box)} (the reported overlap, obscuring both controls)"
        )
    if switcher_shown and _intersects(pill, _as_rect(header_box)):
        violations.append(
            f"the server-switcher band {pill} floats inside the chat header "
            f"{_as_rect(header_box)} (the selector crowds the header's floating controls)"
        )

    assert not violations, (
        "Android server switcher must not float over the chat header's "
        "Web/TUI toggle: " + "; ".join(violations)
    )

    # The web must also have asked the shell to keep the pill hidden — the
    # positive half of the contract, so this can't pass merely because the
    # bridge was never driven.
    assert hidden_calls and all(hidden_calls), (
        "the web must only ever request the native switcher hidden over the "
        "chat surface on an Android shell with the picker surface; got "
        f"setServerSwitcherHidden calls: {hidden_calls}"
    )


def test_android_server_selection_lives_in_the_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Server selection is reachable from the Android navigation drawer.

    The design's other half, mirroring the iOS placement contract: with the
    floating pill kept off the chat surface, the sidebar must carry the
    server picker on a picker-capable Android shell. Opens the drawer from
    the chat header, picks another remembered server from the picker's menu,
    and asserts the switch is requested through the shell bridge with the
    exact URL the picker payload carried.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    _open_android_terminal_first_session(page, seeded_session)

    # The drawer starts closed on a phone viewport; the chat header's toggle
    # is the way in.
    page.get_by_role("button", name="Open sidebar").click()

    picker = page.get_by_test_id("sidebar-server-picker")
    expect(picker).to_be_visible()
    picker.click()

    # The picker's menu offers the other remembered server; choosing it must
    # ask the shell to switch, with the exact URL the picker payload carried.
    page.get_by_role("menuitem", name=_ALT_SERVER_HOST).click()
    page.wait_for_function(
        "() => window.__switchServerCalls.length > 0",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__switchServerCalls") == [_ALT_SERVER_URL], (
        "selecting a server in the sidebar picker must reach the shell bridge "
        "with the exact URL the picker payload carried"
    )

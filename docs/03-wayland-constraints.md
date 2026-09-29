# 03 — Wayland / KDE constraints

The honest limits. Most "computer use on Linux" pain lives here, not in the model.

## 1. Input is focus-routed and cannot be aimed at an arbitrary window

Synthetic input (portal, EIS/libei, ydotool) is delivered to whichever surface currently holds
Wayland input focus. Observed in the field on kwin-mcp issue
[#33](https://github.com/isac322/kwin-mcp/issues/33):

- `wl_keyboard.enter()` recipients are a compositor-enforced boundary. KWin's notion of
  *active window* (taskbar, alt-tab, decorations) and the actual keyboard-focus surface are
  separate things.
- In kwin-mcp 0.7.0, `focus_window` set only AT-SPI focus, so keystrokes went to the wrong app.
  **Fixed in 0.8.0** (`#42`): `focus_window` now activates the target through KWin, and
  discrete scroll reaches the window under the pointer. Reporter-confirmed on KWin 6.7.5.
- Pointer scroll follows the **pointer**, not keyboard focus. Keyboard follows focus.

**Consequences for us**

- A keyboard/text action implies *the agent takes focus*. There is no silent background typing.
- Required pattern for live-desktop work: **save focused window → focus target → act → restore
  original window** before replying to the user. Otherwise the user's next keystrokes land in
  whatever the agent touched.
- Mouse-only work (move + click + scroll under pointer) is the least disruptive mode, and is
  enough for a surprising number of tasks.

## 2. Two escape hatches from the focus problem

1. **Prefer AT-SPI semantic actions over input.** `perform_action` / `set_value` /
   `grab_focus` address an element object directly and often need no real input at all.
   Coverage is per-toolkit and per-widget, and some apps expose a tree but reject activation.
2. **Use an isolated virtual session.** kwin-mcp's `session_start` runs
   `kwin_wayland --virtual` inside `dbus-run-session` — a whole headless KWin desktop for the
   agent, no contention with the user, no focus arbitration. Best for testing/repetition;
   can't reach the user's real apps or real data.

Live desktop and virtual session are complementary modes, not alternatives.

## 3. Browser accessibility trees need per-app probing

Chromium/Electron apps may build their accessibility tree lazily. A useful
probe (from cua-driver) is to set the session-level accessibility flags on the
a11y bus — the same signal a screen reader sends:

- `org.a11y.Status` with `IsEnabled` / `ScreenReaderEnabled` = true

This enabled a useful Firefox and Kate tree on `cachy`, but isolated Brave Origin
still exposed only an empty root, including with `--force-renderer-accessibility`.
The effect is app/build-specific. See the [2026-09-29 P0 measurements](05-open-questions.md#p0-observations--2026-09-29)
and [cua-driver's limits](https://cua.ai/docs/reference/cua-driver/limits).

## 4. Toolkit shapes differ; there is no single "click this element"

GTK3 ≠ GTK4 ≠ Qt5 ≠ Qt6 ≠ Tk ≠ Chromium. Some widgets accept text via accessibility, some need
`GrabFocus` without raising the app, some need synthetic focus events, some need a click first.
cua-driver's per-toolkit focus-free write paths are the reference work here. Expect to build a
small router, not a recipe.

## 5. Capture paths

- `kwin-mcp` currently tries KWin `org.kde.KWin.ScreenShot2`, then Spectacle. Live
  ScreenShot2 access depends on the KWin D-Bus restricted-interface whitelist. Test
  this path in both live and virtual sessions before selecting a fallback. See
  [`kwin-mcp` limitations](https://github.com/isac322/kwin-mcp/blob/main/README.md#limitations).
- KDE portal `org.freedesktop.portal.ScreenShot` is a separate possible fallback;
  do not assume it is the driver's normal path or prompt-free on this host.
- Video / continuous frames: portal `ScreenCast` + PipeWire.
- For our loop, screenshots should be **verification only** (diff / crop), not decision input —
  text state is cheaper and Jev cannot look at pixels anyway.

## 6. Practical environment checklist for `cachy`

- [x] AT-SPI registry running; session flags can be enabled for a task
- [x] `org.a11y.Status` flags tested and restored to their original false values;
      Firefox/Kate trees appeared, isolated Brave's did not (2026-09-29)
- [x] `/dev/uinput` accessible by `messhias`; `ydotoold` running (input behavior untested)
- [x] KWin EIS reachable in live and virtual sessions with `kwin-mcp==0.10.0`
- [x] `kwin-mcp screenshot` worked in live and virtual sessions through ScreenShot2
      (capture latency not measured)
- [x] `spectacle` present as a screenshot fallback (observed 2026-09-29)
- [x] KWin version noted: 6.7.5 on `cachy` (2026-09-29); private EIS D-Bus can move

## 7. Remote callers amplify §1 (added for the tailnet endgoal)

The focus problem is *worse* when the caller is not physically at the machine:

- A remote agent driving the live desktop **takes over the keyboard** with nobody in the room to
  notice. The save → act → restore pattern is not a nicety here, it is the difference between a
  usable machine and a hijacked one.
- Nothing in Wayland tells us whether the human is typing right now. We have to infer it (input
  idle time via AT-SPI/`org.kde` activity or an idle-inhibit watchdog) and refuse live tasks
  while the owner is active, unless explicitly overridden.
- Remote verification must not mean "stream the screen". Send metadata and a hash; downscale a
  single JPEG on request. Screencast over the tailnet is not a supported path.
- Latency compounds: one tailnet round trip per step adds to the Jev call.
  The 12-pair synthetic test from `cachy` measured 254 ms median for TypeSafe
  direct and 839 ms for the hosted gateway. Prefer one task call with the loop on the host
  (ADR-008/009); measure representative desktop states before setting budgets.

## 8. Known hard cases (accept as out of scope initially)

Canvas/games/GPU surfaces, DRM-protected content, nested/remote sessions, drag-and-drop across
apps, file pickers that use portal dialogs, apps with no accessibility implementation at all
(some Electron builds, custom toolkits, TUI apps pretending to be windows), multi-monitor
HiDPI coordinate translation, and anything requiring text entry into a terminal.

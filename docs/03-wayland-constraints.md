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

## 3. Chromium / Electron apps have no AT-SPI tree until they think a screen reader is present

Chrome, VS Code, Slack, Discord, Obsidian and all Electron/CEF apps build their accessibility
tree lazily. The fix (per cua-driver) is to set the session-level accessibility flags on the
a11y bus — the same signal a screen reader sends:

- `org.a11y.Status` with `IsEnabled` / `IsScreenReaderEnabled` = true

Chromium then builds the tree **retroactively** for already-running apps; GTK/Qt warm up their
a11y paths on the same signal. On GNOME a `gsettings` toggle may also be needed; on KDE the
System Settings → Accessibility toggle should be on. This one trick is most of the difference
between "brittle" and "workable" for a modern desktop.

## 4. Toolkit shapes differ; there is no single "click this element"

GTK3 ≠ GTK4 ≠ Qt5 ≠ Qt6 ≠ Tk ≠ Chromium. Some widgets accept text via accessibility, some need
`GrabFocus` without raising the app, some need synthetic focus events, some need a click first.
cua-driver's per-toolkit focus-free write paths are the reference work here. Expect to build a
small router, not a recipe.

## 5. Capture paths

- Screenshot: KDE portal `org.freedesktop.portal.ScreenShot`, or `spectacle -b -n -o out.png`.
  The portal may show a consent prompt; a restore token makes it persist.
- Video / continuous frames: portal `ScreenCast` + PipeWire.
- For our loop, screenshots should be **verification only** (diff / crop), not decision input —
  text state is cheaper and Jev cannot look at pixels anyway.

## 6. Practical environment checklist for `cachy`

- [ ] AT-SPI registry running; accessibility enabled in KDE System Settings
- [ ] `org.a11y.Status` flags set (unblocks Chromium/Electron trees)
- [ ] `/dev/uinput` accessible by user for ydotool fallback (`ydotoold` running)
- [ ] KWin EIS reachable (kwin-mcp `session_connect` reports "Input backend: KWin EIS")
- [ ] Portal screenshot works without a prompt loop (`restore_token` cached)
- [ ] `spectacle` present as a screenshot fallback
- [ ] KWin version noted — private EIS D-Bus can move between releases

## 7. Known hard cases (accept as out of scope initially)

Canvas/games/GPU surfaces, DRM-protected content, nested/remote sessions, drag-and-drop across
apps, file pickers that use portal dialogs, apps with no accessibility implementation at all
(some Electron builds, custom toolkits, TUI apps pretending to be windows), multi-monitor
HiDPI coordinate translation, and anything requiring text entry into a terminal.

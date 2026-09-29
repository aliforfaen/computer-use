# 02 — Prior art

Who already does parts of this. The point of this doc: **what we must not rebuild.**

## A. Jev-side patterns (the decision loop)

| Project | What it gives us |
| --- | --- |
| [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast) | The canonical loop: indexed element table → one Jev request with `action` + `target` heads → code executes → validate. Documents the guardrails ("model output never becomes selectors, coordinates, shell commands or executable JS"). Read `agent.py` + `questions.py` before designing ours. |
| Jev + WebMCP benchmark (`nekuda-ai/WindTunnel`) | Evidence that Jev is **much** stronger when the option space is a small set of explicit actions than when it must choose a click sequence. 49/49 with tools vs 25/49 without, on their benchmark. Suggests we should offer *tasks* where possible, not raw clicks. |
| macOS Jev computer-use demos | Two flavours: (a) OCR-only, no screenshots leave the machine; (b) local CoreML segmentation + on-device OCR → Jev picks an element, ~90 ms/decision. Both confirm "text-only state" is enough for the decision. |
| Stagehand + Jev | a11y tree as `state`, actions as `questions`. Same shape, web-only. |

Takeaways for us: two decision heads in one request; speculative targets; text typed only via
a small LLM; re-validate before executing.

## B. Linux desktop drivers (the OS plumbing — reuse these)

| Project | Lang / OS | What it covers | Relevance |
| --- | --- | --- | --- |
| [`isac322/kwin-mcp`](https://github.com/isac322/kwin-mcp) | Python, MIT, PyPI | **KDE Plasma 6 Wayland first-class.** AT-SPI2 tree, 33 MCP tools, KWin private **EIS/libei** input injection (no portal prompts), clipboard, window management, screenshots. Isolated virtual sessions (`kwin_wayland --virtual` in `dbus-run-session`) **and** live-session attach. | Closest match to our target. Strongest candidate backend. |
| [`agent-sh/computer-use-linux`](https://github.com/agent-sh/computer-use-linux) | Rust, MIT, crates.io + npm | AT-SPI + portal screenshots + `RemoteDesktop` portal / ydotool input + multi-compositor window registry (incl. a KWin DBus backend). `doctor` returns a JSON readiness report. 15 MCP tools. | Best "is this machine even ready" diagnostics. Validated on GNOME; KDE path coded but less battle-tested. |
| [`BeckhamLabsLLC/linux-desktop-mcp`](https://github.com/BeckhamLabsLLC/linux-desktop-mcp) | Python, MIT | AT-SPI2 `ref_N` semantic references, natural-language element search, window targeting with colour overlays, xdotool/ydotool backends. | Good reference for the *state compiler* and ref lifecycle/GC. Simpler; not KDE-tuned. |
| [`trycua/cua`](https://github.com/trycua/cua) (cua-driver) | Rust | AT-SPI + XTEST, background cursor, focus-free write paths per toolkit (GTK3/4, Qt5, Tk). X11/XWayland supported; native Wayland preview. | The toolkit-specific write paths are the deepest prior art. Also: it flips session `org.a11y.Status` so Chromium/Electron apps build their AT-SPI tree retroactively — copy that trick. |
| [`OmniParser`](https://github.com/microsoft/OmniParser) (v2) | Python | Screenshot → labelled element boxes (YOLO + Florence). | Fallback for canvas/games/non-AT-SPI clients. Costs a model call; keep it off the hot path. |

## C. Input-injection building blocks

- **KWin private EIS D-Bus** — what kwin-mcp uses. No consent dialog, routes through the
  focused surface. Private interface: can break on KWin upgrades.
- **`xdg-desktop-portal` RemoteDesktop** — the sanctioned path. KDE implements it and has
  `ConnectToEIS` merged; requires a user consent dialog per session (persistable via restore
  token). `NotifyPointerMotion`/`NotifyKeyboardKeycode` when not using EIS.
- **`ydotool` / uinput** — kernel-level HID emulation. Focus follows normal physical-input
  rules, works where EIS misroutes, needs `/dev/uinput` access (ACL on CachyOS is usually fine).
  Still on kwin-mcp's roadmap (M11).
- **`wtype`** — uses `zwp_virtual_keyboard_unstable_v1`, which **KWin does not implement**
  (KDE bug 502882). Not an option on Plasma.
- **`kdotool`** — xdotool-like window control for KWin (activate, geometry, list). Complements input.
- **Screenshots** — KDE portal `ScreenShot`, or `spectacle -b -n -o <file>`; screencast via
  portal `ScreenCast` + PipeWire when video is needed.

## D. What is genuinely missing (candidate scope for us)

1. **A KDE-native state compiler**: AT-SPI tree → compact, deduplicated, budget-capped
   candidate table with stable indices and app-aware filtering. No project does this well;
   it is where accuracy is actually won or lost.
2. **A Jev decision policy for desktop apps**: action head + target head + `noul` risk/done
   gates, with a strict "model proposes, code disposes" boundary.
3. **Verification wrappers**: post-condition checks (AT-SPI property re-read, focused-window
   check, screenshot diff) around each action.
4. **A focus/session arbiter**: because Wayland input is focus-routed, someone has to decide
   when the agent may take focus and how it restores the user's window afterwards.

Everything else in layers 0 and 3 is reusable today.

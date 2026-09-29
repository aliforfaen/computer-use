# 09 — Structured observation options

**Research, 2026-09-30; no architecture decision.** The P2 KCalc proof
parses `kwin-mcp==0.10.0`'s human-readable AT-SPI tree. That is a fragile
interface for a general observer. The underlying data is already structured.

| Route | What it buys | Cost or limit |
| --- | --- | --- |
| Direct [`libatspi` through PyGObject](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/) | Typed accessible objects, roles, states, text, actions and bounds; [recommended by GNOME over pyatspi for new Python clients](https://github.com/GNOME/pyatspi2/blob/master/README.md). | Must join the virtual session's AT-SPI bus and map window-relative bounds to KWin coordinates; synchronous calls need timeouts. |
| `kwin-mcp` 0.10.0 internal worker | Its private `find`/`wait` operations already exchange JSON element records inside the driver. | Private protocol; public MCP accessibility tools return formatted strings (even if MCP wraps the string in structured content). Version pinning alone does not make it stable. [Upstream code](https://github.com/isac322/kwin-mcp/tree/main/src/kwin_mcp). |
| [KDE Selenium AT-SPI](https://develop.kde.org/docs/apps/tests/appium/) | Existing WebDriver semantics and accessibility actions for Qt/GTK apps. | Another server and protocol; assess if its element identity and session handling suit an open-ended agent before adopting. |
| [linux-desktop-mcp](https://github.com/BeckhamLabsLLC/linux-desktop-mcp) | Semantic `ref_N` pattern and click-by-reference example. | Another input/session backend; references still need freshness checks and effect verification. |

The proposed next measurement is a **throwaway direct-Atspi probe** in an
isolated KCalc session: get typed One-button and editable-display records,
invoke the button's [`Action`](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Action.html)
interface if it works, and re-read the display. Compare its bounds with
KWin window geometry; mark coordinates unavailable if the mapping is
ambiguous. Record KWin, `kwin-mcp`, and AT-SPI versions and latency. A second
app should test whether object identity survives a UI change. These results
would decide whether to use direct Atspi or ask `kwin-mcp` upstream for a
public JSON observation tool.

Even a typed observer does not solve task planning. [`jev-ultrafast`](https://github.com/browser-use/jev-ultrafast)
uses Jev for each bounded operation/target decision, while code maintains
the loop, validates targets and checks effects. For open-ended desktop tasks,
a capable agent or planner would need to own the task goal and interpret
progress; Jev could remain an optional fast selector for a well-formed
candidate set. A generic adapter can check target freshness and visible
effects, but cannot prove arbitrary user goals without a task-specific
postcondition or a planner evaluating the new observation. This is a product
choice to test, not a claim that a better parser makes Jev autonomous.

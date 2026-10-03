# 28 — Tray indicator, user service and config

**2026-10-03.** Owner-facing surfaces for the existing single-owner local
service. No new owner capability, transport or policy is added: the tray is
another client of the same private Unix socket.

## Config file (no secrets)

The daemon resolves settings with CLI > `JEV_DESKTOP_*` env > config file >
built-in default. CLI flags that are not given are no longer forwarded with
hard-coded defaults, so the config can supply them.

Path: `$JEV_DESKTOP_CONFIG`, else `--config`, else
`${XDG_CONFIG_HOME:-~/.config}/jev-desktop/config.json`.

```json
{
  "allowed_apps": ["kate", "firefox", "kcalc"],
  "run_dir": "/run/user/1000/jev-desktop",
  "idle_timeout": 180,
  "max_session_lifetime": 1800,
  "max_actions": 64,
  "max_observations": 256,
  "audit_max_bytes": 4194304,
  "audit_backups": 3,
  "audit_max_age_days": 30,
  "reader": {"provider": "deepseek", "key_env": "DEEPSEEK_API_KEY", "max_calls_per_session": 8}
}
```

Unknown keys are rejected so typos fail fast, keys and list types are checked,
and an empty allowlist still fails closed whether it comes from flags, env or
file (`JEV_DESKTOP_ALLOW_APPS` accepts commas or whitespace). **The config
carries no secret values**: a reader key stays in the environment or the
configured `dotenv` file; the config only names the environment variable.

## Audit retention

The audit is durable state, so it defaults to
`~/.config/jev-desktop/audit.jsonl` (0600) rather than the runtime directory
where a logout would lose it. All owner writers share one append path:
newline-terminated JSON, fsynced, size-rotated to `audit.jsonl.1..N`
(default 4 MiB, 3 backups) and age-pruned (default 30 days). Stale session
journals under `sessions/` are swept at startup with the same age limit. The
socket and live session journals stay in the private runtime directory, and the
recorded fields and no-sampling rule are unchanged. `capabilities` reports the
path and the active rotation. See ADR-021.

## systemd --user unit

The unit is rendered from a single template embedded in
`desktop_service_unit.UNIT_TEMPLATE` (no extra data file to drift or to ship in
a wheel). `jev-desktop service install` resolves the `jev-desktop` entry point
and the config path into `${XDG_CONFIG_HOME:-~/.config}/systemd/user/` and runs
`daemon-reload`. It never enables the unit.

```ini
[Unit]
PartOf=graphical-session.target
After=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
Type=exec
ExecStart=<entry point> daemon --foreground --config <config>
Restart=on-failure
TimeoutStopSec=30
KillMode=mixed
NoNewPrivileges=yes
EnvironmentFile=-%h/.config/jev-desktop/daemon.env
```

- `SigTERM` is already handled: the owner stops the session, restores focus and
  accessibility, closes only its owned app, removes its socket and exits 0, so
  `KillMode=mixed` and the 30 s stop timeout are enough and an intentional
  `shutdown` does not look like a crash to `Restart=on-failure`.
- **The kill switch does not change**: `systemctl --user stop jev-desktop`
  and/or `jev-desktop stop --all`, plus `tailscale serve off` as the hard stop.
  The unit adds a lifecycle, not a new bypass; loopback-only binding,
  deny-by-default allowlist and audit logging are untouched.
- Host check (2026-10-03): `graphical-session.target` is active,
  `WAYLAND_DISPLAY=wayland-0` is in the user manager environment.

Commands (all emit one JSON object):

```bash
uv run jev-desktop service install --allow-app kate --allow-app firefox
uv run jev-desktop service status
uv run jev-desktop service uninstall --stop
```

`install` seeds a `0600` starter config only when none exists and refuses to
overwrite or "fix" an allowlist the owner already edited. `uninstall` refuses
to remove an active unit without `--stop`, because stopping the owner closes
its owned app. `status` reports active/enabled state, the config allowlist and
template/entry-point drift for both units.

`service install --tray` also writes `jev-desktop-tray.service` (same
`graphical-session.target` gating, `Restart=on-failure`). It is never enabled
automatically either, and `uninstall` removes both units.

## Tray settings

Tray behaviour lives in `~/.config/jev-desktop/tray.json` (0600):

```json
{
  "desktop_mode": "virtual",
  "autonomy_mode": "guarded",
  "poll_seconds": 3.0,
  "confirm_kill_switch": true,
  "confirm_cleanup": true
}
```

Missing or invalid values fall back to the defaults, and the Settings… dialog
edits the same file. The live-mode gate is deliberately **not** configurable:
`desktop_mode` only decides what a tray-started task requests, and a physical
task still opens the per-task owner-present + temporary-accessibility dialog.
"Open config folder" hands the directory to `xdg-open`.

## Tray indicator

`jev-desktop tray` (or the `jev-desktop-tray` script) runs a KDE
StatusNotifierItem via GTK3 + `AyatanaAppIndicator3`. It polls `status` and
`capabilities` every 3 s in a worker thread and marshals updates onto the GTK
loop; every owner or `systemctl` call is off the GTK thread.

Menu: owner/session/service status lines, a desktop-mode radio
(`virtual` default / `physical`), `Start task` per allowlisted app, `Clean up
session and owned apps` (`stop_all` with the cleanup confirmation surfaced),
a `Service` submenu (start/stop/enable-at-login/disable-at-login), the
`Stop all and shut down owner` kill switch, and `Quit tray` (tray only).

Mode is a **preference**, never an authorization. Selecting `physical` only
changes which mode a tray-started task requests; an actual live start always
opens a per-task dialog that must confirm the owner is present **and** that
temporary accessibility is allowed. Only then are `owner_present_override` and
`temporary_a11y` sent, so the ADR-018 gate is preserved rather than relaxed.
The preference lives in `~/.config/jev-desktop/tray.json` (`0600`).

Host evidence (2026-10-03): with the tray running, KDE's
`org.kde.StatusNotifierWatcher` `RegisteredStatusNotifierItems` grew by exactly
one entry (`:1.245/org/ayatana/NotificationItem/jev_desktop`) and returned to
its prior value after exit. The tray coexisted with a sandbox owner and logged
no traceback. libayatana prints a deprecation notice recommending
`libayatana-appindicator-glib`; the GTK3 binding is what builds here.

## Platform dependencies

- Python: `pygobject` is now an explicit dependency (it was already transitive
  through kwin-mcp). `dbus-python` remains kwin-mcp's.
- System: GTK3 typelibs and `libayatana-appindicator` for the tray, plus the
  existing Wayland/D-Bus/AT-SPI set. `service` and the daemon need none of the
  tray stack.

## Verified and not verified

Verified: config precedence and fail-closed allowlist, install/status/
uninstall behaviour including drift and the active-unit refusal, tray-unit
install, audit rotation and pruning, tray settings round-trip, tray SNI
registration and coexistence with a sandbox owner, live-gate decisions and
kill-switch confirmation (stubbed toolkit), and the full affected suite
(177 cases, all green).

Not verified: clicking every tray item on screen, enabling a unit at login by
the project, a live task started from the tray, and the tray under a
non-Plasma desktop.

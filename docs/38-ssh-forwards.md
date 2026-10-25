# 38 — Named SSH port forwards

This module provides an optional, local manager for named OpenSSH TCP forwards.
It does not use an LLM, construct a protocol proxy, or alter the desktop owner
daemon. OpenSSH remains responsible for authentication and transport, using the
user's existing `~/.ssh/config`, keys and agent.

## Configuration

Store a private JSON file at
`${XDG_CONFIG_HOME:-~/.config}/jev-desktop/ssh-forwards.json`:

```json
{
  "forwards": [
    {
      "name": "local-mcp",
      "ssh_host": "my-tailnet-host",
      "direction": "local",
      "local_port": 8765,
      "endpoint": "127.0.0.1:8765",
      "endpoint_path": "/mcp"
    },
    {
      "name": "remote-mcp",
      "ssh_host": "my-tailnet-host",
      "direction": "reverse",
      "forwarded_port": 8766,
      "endpoint": "127.0.0.1:8766",
      "endpoint_path": "/mcp",
      "ssh_config": "/home/me/.ssh/config"
    }
  ]
}
```

`direction: local` means a local listener forwards to `endpoint` as seen from
the SSH server (`-L`). It requires `local_port` and omits `forwarded_port`.
`direction: reverse` asks the SSH server for a listener which forwards to
`endpoint` as seen from this computer (`-R`); it requires `forwarded_port` and
omits `local_port`. `endpoint_path` is an optional absolute URL path (default
`/mcp`). `ssh_config` is an optional absolute OpenSSH config file path passed
as `-F`. Endpoints are TCP `host:port` values and must be loopback targets
(`localhost`, `127.0.0.1`, or `[::1]`). Names, ports, hosts, endpoint syntax
and JSON keys are checked before use; duplicate names and unknown keys fail
closed. Keep credentials out of this JSON. Configure identity and host aliases
in OpenSSH's normal private configuration.

Both listeners bind explicitly to loopback: `127.0.0.1` locally for `-L`, and
`127.0.0.1` remotely for `-R`; `GatewayPorts=no` is also set. The remote
listener still depends on the SSH server's forwarding and `GatewayPorts`
policy, so the client request alone does not prove or guarantee the remote bind
address. Reverse forwarding also requires the SSH server to permit it. The generated SSH command uses
`BatchMode=yes`, `ExitOnForwardFailure=yes`, `-N -T -n`, and encrypted-channel
keepalives. OpenSSH documents that `ExitOnForwardFailure` confirms forwarding
setup, but does not guarantee that connections to the ultimate endpoint will
succeed ([ssh_config(5)](https://man.openbsd.org/ssh_config),
[ssh(1)](https://man.openbsd.org/ssh)).

## Python manager API

The tray can use `SSHForwardManager` from `jevdesktop.ssh_forwards`:

```python
manager = SSHForwardManager()
entries = manager.list()
manager.add({"name": "local-mcp", "ssh_host": "my-tailnet-host",
             "direction": "local", "local_port": 8765,
             "endpoint": "127.0.0.1:8765"})
manager.edit("local-mcp", {"ssh_host": "my-tailnet-host",
                            "direction": "local", "local_port": 8765,
                            "endpoint": "127.0.0.1:8765",
                            "endpoint_path": "/mcp"})
manager.delete("local-mcp")  # only when no unit is installed and state is inactive
manager.remove("local-mcp")  # safely remove inactive unit and entry
manager.install("local-mcp")
manager.enable("local-mcp")  # explicit boot/session persistence
manager.start("local-mcp")   # explicit connection start
state = manager.status("local-mcp")
report = manager.snapshot()  # all forwards; read-only monitoring data
manager.stop("local-mcp")
manager.disable("local-mcp")
manager.uninstall("local-mcp")
```

Install writes a per-forward user unit and reloads the user manager; it does
not enable or start it. Enable and start are separate calls. Uninstall refuses
to remove an active unit; stop it first. Units are named
`jev-ssh-forward@<escaped-name>.service`; name escaping follows systemd's unit
name encoding rules ([systemd.unit(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)).
Arguments are built as an argv list, validated, then quoted for systemd's
`ExecStart` syntax; nothing is evaluated by a shell.

`add(entry)` validates and atomically replaces the config with a `0600` file;
the parent config directory is created with mode `0700`. `edit(name, entry)`
replaces the selected entry while retaining its name, but only after its unit is
uninstalled. `delete(name)` also requires an uninstalled unit and definitely
inactive state.
`remove(name)` is the UI-friendly combined operation: it refuses an active unit,
uninstalls an inactive installed unit, then removes the config entry. `edit`
is refused while a unit file exists or systemd state is active or unknown.
Uninstall first, edit the entry, then explicitly install the updated unit.
`status` includes `installed`, `active_state`, `sub_state`, `load_state`, and a
`local_url` for local forwards. `process_active` is false for known inactive or
failed units, true for active units, and unknown (JSON `null`) for transitional
or unavailable states. A recognized `LoadState=not-found` with no active state
is treated as definitely absent even when `systemctl show` returns a nonzero
status for a missing unit ([systemctl exit status](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)); an unavailable manager remains
unknown. Install refuses to overwrite an active or unknown unit. Uninstall and
combined Remove refuse unknown state and preserve the unit file if disabling
fails.

Status reports the systemd process state separately from local listener
reachability. A reachable local listening socket is only a bind check, not a
health check of the ultimate MCP endpoint. Reverse forwarding's listener is on
the remote SSH server and is not probed from here. In both directions,
`endpoint_health` remains `not_checked`: an active SSH process can coexist with
an unavailable application endpoint.

`snapshot()` returns an aggregate `checked_at` timestamp and one independent
record per configured forward. Each record includes lifecycle/process state,
TCP reachability when a local listener can be probed, `mcp_health: not_checked`,
the in-scope local or remote URL, and a bounded sanitized `last_error`. One
failed systemd query does not hide other entries. `stale` is false for a newly
queried snapshot; callers should compare its `checked_at` with current time when
using cached relay data. This snapshot never connects to an SSH host or sends
MCP `initialize`/`tools/list` requests. The owner can expose it through the
read-only relay tool so an agent can inspect forwards without getting lifecycle
controls. Systemd monitoring queries each have a two-second timeout and report
only bounded state properties: result enum, exit status, restart count and
active/inactive timestamps. An `auto-restart` substate is reported as
`lifecycle_state: reconnecting` while `process_active` remains unknown; no
journal text is returned.

The tray exposes add, edit, remove, install, start, stop, enable, disable and
uninstall controls. It never starts or enables a tunnel automatically; those
are explicit owner actions. No tunnel was enabled or started while implementing
this manager. There are no separate CLI commands.

The managed SSH process disables background forking and connection multiplexing
so its lifetime belongs to its unit. Authentication must already work
non-interactively; this feature does not provision SSH keys or remote accounts.

# 06 — Remote agents over the tailnet

Endgoal: **Hermes assistants (and other remote agents) connect to this desktop layer over the
tailnet.** Not every caller runs locally. This doc is the remote half of the design.

## Topology (ADR-005)

```
   LOCAL                              REMOTE (tailnet)
   Pi / Claude Code                   Hermes @ VPS
   ──── stdio ────┐                   Hermes @ GPU box
                  │                   any tailnet node
                  ▼                        │
        ┌──────────────────────────────────┴──────────┐
        │  cachy · jev-desktop (single instance)      │
        │  MCP stdio        +  MCP Streamable HTTP    │
        │  policy engine · guardrails · audit log     │
        └───────────────┬─────────────────────────────┘
                        │ 127.0.0.1:7810 only
                        ▼
              tailscale serve  (https://cachy.<tailnet>.ts.net/mcp)
                        │
                   tailnet ACLs  ← the only access gate (ADR-007)
```

One policy engine instance, one allowlist, one audit log. The local stdio command
needs to forward to that instance; otherwise each client would start a second
policy engine. `jev-desktop` owns the HTTP endpoint and a persistent `kwin-mcp`
stdio child (ADR-009).

**Why one instance:** the thing being controlled is a single desktop. A second instance on the
same desktop would race for focus; an instance per node is a different product (fleet control),
out of scope until there is a second desktop worth driving.

## Transports

| Caller | Transport | Notes |
| --- | --- | --- |
| Local agents | MCP **stdio** | No network, no auth surface. Default for Pi/Claude Code/Codex. |
| Remote agents | MCP **Streamable HTTP** | Spec revision 2026-07-28. One endpoint, `POST` per message, `Accept: application/json, text/event-stream`. |

- Hermes supports both natively — its `mcp_servers` config accepts either `command:` (stdio) or
  `url:` + `headers:` (HTTP) with TLS settings. **No stdio→HTTP bridge needed.**
- We never expose the SSE-only 2024-11-05 transport; Streamable HTTP is the current one.
- `tailscale serve` gives us HTTPS with a tailnet cert (`cachy.<tailnet>.ts.net`) and rejects
  traffic from outside the tailnet. **Never use `tailscale funnel` for this** — Funnel is
  public and, by design, strips identity headers.

## Access model (ADR-007 — owner decision)

**Tailnet ACLs are the only access gate. No per-client tokens, no OAuth.** Recorded as chosen.
Consequences, stated plainly so they are not rediscovered later:

- **Any device that can reach the Serve URL controls the desktop.** That includes a compromised
  VPS running Hermes, any node added to the tailnet later, and any device you accept a *share*
  from — Tailscale populates identity headers for external share users, and ACLs decide reach.
- **Identity headers are unavailable to us here.** Serve adds `Tailscale-User-Login`,
  `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`, and it *strips* client-supplied copies to
  prevent spoofing — but they are **not populated for tagged devices**, which is the common shape
  for a cloud/VPS node. So we cannot rely on them for caller identity.
- **Caller-node identification is still a P0 measurement.** HTTPS Serve proxies
  to localhost, so the backend socket peer may be Serve itself rather than the
  original node. Probe headers and peer address from a user-owned and a tagged
  remote node before assigning a caller-node value in the audit log. If identity
  is unavailable, record `unknown` rather than infer it from the proxy address.
  See [Tailscale Serve's proxy behavior](https://tailscale.com/docs/features/tailscale-serve).

### Required compensating controls (these ship, not the auth)

Because authorization is coarse, behavior control has to be fine:

1. **Bind to `127.0.0.1` only.** Serve proxies to localhost. This is what makes "tailnet ACL is
   the gate" actually true — otherwise anything on the LAN could call it directly and forge headers.
2. **Deny-by-default app allowlist in every mode**, including `yolo` (ADR-003). This is now the
   main blast-radius limiter, so it must be explicit config, not a default-open list.
3. **Append-only audit log** (JSONL): caller node, transport, tool, autonomy mode, Jev answers
   with confidences, action taken, verification outcome, timestamp. Every call, no sampling.
4. **Kill switch, reachable locally**: `jev-desktop stop --all` aborts the running session and
   revokes remote sessions. Plus `tailscale serve off` as the hard stop — it must remain a
   one-liner the owner can type without thinking.
5. **Step caps and a wall-clock budget per remote task.** A remote `yolo` loop cannot run
   forever by accident.
6. **No secrets in state.** The compiler must never include clipboard contents, window titles of
   secret stores, or typed text in the state sent to Jev, because state logs are auditable but
   also readable by whoever gets the log.

### Cheap upgrade path (do not build now, leave the seam)

The auth decision is a *policy* decision, not a protocol one. Keep an `authorizer` interface with
one implementation today (`tailnet_acl`, i.e. allow all calls that arrive) so that adding either
of these later is a config change, not a rewrite:

- **Per-client bearer token**: a `headers:` entry in the client config; server compares a hash.
- **Tailscale app capabilities** (`--accept-app-caps`, client ≥1.92): grant `desktop-control` as
  an explicit capability per user or tagged node; Serve forwards `Tailscale-App-Capabilities`
  and strips forged copies. This is the cleanest eventual model and works for tagged devices.

## Session modes for remote callers (ADR-006 — owner decision)

Owner chose **"remote sees everything"**: remote callers have the same reach as local, including
the live desktop and `yolo`. Design notes that follow from that:

- Default remains the **virtual** session (`kwin_wayland --virtual`) for anyone who does not ask
  for live — a remote agent gets a sandbox unless it says otherwise.
- `live` must be **requested explicitly per task** and is recorded as such in the audit log.
- Live + remote + `yolo` is the highest-risk configuration in the whole system. It is allowed,
  but it is the case where a supporting control earns its keep: **focus save → act → restore**
  is mandatory, and the session must refuse to start if the owner's physical input has been
  active in the last N seconds (idle check), unless overridden.
- Concurrency: one live-desktop task at a time, enforced by a lock. Virtual sessions can run in
  parallel — each remote client gets its own sandbox if it wants one.

## Latency, bandwidth, and why the loop stays server-side

**ADR-008 confirmed in ADR-009:** the full observe→decide→act→verify loop runs on `cachy`,
and remote callers mostly call one high-level tool per task.** Rationale:

- A per-step remote loop pays a tailnet round trip *plus* a Jev call per step.
  A per-task remote loop pays one round trip. A 12-pair synthetic test on
  2026-09-29 measured 254 ms median direct and 839 ms through the gateway;
  representative desktop-state latency remains to be measured.
- Both provider keys stay on `cachy`. Remote agents never hold them.
- Guardrails cannot be bypassed by a remote caller, because the caller never drives the executor
  directly.

Low-level primitives (`observe`, `act`, `verify`) may remain available for
composability, but they must share the server's session owner and cannot
interleave actions with an active task.

Bandwidth notes:

- State is compact text; a desktop step is single-digit KB. Fine over WireGuard.
- **Do not ship screenshots to remote callers by default.** Return metadata (dimensions, changed
  regions, a hash) and a downscaled JPEG only on explicit request. Video/screencast over the
  tailnet is off the table by default.
- Check `tailscale ping cachy` early: a **direct** WireGuard path is ~10–50 ms and irrelevant; a
  **DERP-relayed** path adds ~100 ms+ and changes whether a per-step loop feels acceptable.

## Hermes client config (shape, not a secret)

```yaml
mcp_servers:
  cachy_desktop:
    url: "https://cachy.<tailnet-name>.ts.net/mcp"
    headers:
      # no credential under ADR-007; this is where a bearer token would go later
      X-Client: "hermes-vps"
    tls:
      ssl_verify: true
```

Local agents use the stdio form (`command:` + `args:`) pointing at the same binary.

## Open remote-specific questions

1. Should remote callers be *told* which mode they got? Probably yes — a `session` tool returning
   mode, allowlist, step budget and remaining budget.
2. Do we want a per-calling-node allowlist of tools (a VPS may need observation but not typing)?
   Cheap to add on top of the app allowlist; worth deciding once a second remote agent exists.
3. Does the owner want a notification (ntfy/Telegram) on every remote live-desktop task start?
   Given the permissive auth decision, this is the cheapest honest safeguard.

# Discord Commander Security and Reliability Audit

> Review date: 2026-08-08
>
> Scope: Python application, Dockerfile, Compose configuration, and repository-managed defaults.
>
> Out of scope: live VPS, Discord server configuration, Infisical policy, private routing, MikroTik,
> NAS, edge host, and deployed SSH keys/host keys.

This is a code/configuration review, not proof of the live environment. The system topology is in
[ARCHITECTURE.md](./ARCHITECTURE.md), while implemented runtime behaviour is in
[WORKFLOW.md](./WORKFLOW.md).

## 1. Executive summary

The repository has a deliberately narrow interaction surface: guild-scoped slash commands and
buttons, a control-room channel gate, an explicit user allowlist, fixed remote scripts, validated
configuration, SSH subprocess timeouts, and a separate watchdog-only channel. Wake and shutdown add
requester-bound confirmation, a per-user rate limit, and a shared power lock.

The most important outstanding risks are deployment hardening rather than an open Discord command
injection path:

- SSH endpoint identities are not pinned across connections.
- The container sees the VPS account's entire SSH directory and runs as root.
- The current rate limit protects power actions only.
- Operational and monitor state is in memory and resets on restart.
- `infisicalsdk` is not version-pinned in `requirements.txt`.

No critical repository-level issue was identified. The items below should be resolved or consciously
accepted before calling the bot a hardened control plane.

## 2. Scope, assets, and assumptions

### 2.1 Assets

- Discord bot token, guild/channel IDs, and operator allowlist.
- Infisical Universal Auth credentials.
- SSH private keys and known-host data mounted into the container.
- MikroTik access for ping and Wake-on-LAN.
- NAS access for boot-time lookup and graceful shutdown.
- Internal edge-host information-script access.
- NAS availability/power state and watchdog notification integrity.

### 2.2 Relevant actors

- A Discord user outside the allowlist or in the wrong channel.
- A compromised authorized Discord account.
- A Discord administrator or bot-token holder.
- A compromised VPS/container or a process able to read mounted SSH data.
- A network-path attacker attempting SSH interception.
- An Infisical user/service able to alter configuration values.

### 2.3 Required external assumptions

The code relies on controls that cannot be verified here:

- the bot is installed only in intended Discord guilds and has minimum channel permissions;
- the home devices are not exposed directly to the public internet;
- routed private-network paths are restricted to intended systems;
- SSH accounts, keys, and `sudoers` policies are least privilege;
- Infisical Universal Auth is limited to the intended project, environment, and path;
- VPS host/Docker security and mounted secret permissions are maintained;
- the remote scripts are trusted and not writable by automation users.

## 3. Verified repository controls

| Control | Evidence | Coverage |
|---|---|---|
| Guild restriction | `DISCORD_GUILD_IDS` checked for every interaction | Slash commands and panel clicks |
| Control-room restriction | `DISCORD_CONTROL_ROOM_CHANNEL_ID` checked for every interaction | Slash commands and panel clicks |
| User allowlist | `DISCORD_ALLOWED_USER_IDS` checked for every interaction | Slash commands and panel clicks |
| Fail-closed interaction rejection | Ephemeral denial plus warning log | DMs, wrong guild/channel, unauthorized users |
| Guild-scoped command registration | Explicit sync for allowed guild objects | Limits command publication scope |
| No message-content surface | `Intents.none()` plus guild interactions only | Avoids arbitrary text-command parser |
| Separated alert destination | Watchdog channel required and must differ from control room | Automatic alert routing |
| No mass mentions from watchdog | `AllowedMentions.none()` | All automatic Discord alerts |
| Requester-bound power confirmation | Confirmation view checks initial user and repeats authorization | Wake/shutdown execution |
| Power-action mutual exclusion | One `PowerOperationState` lock | Wake vs. wake, shutdown vs. shutdown, wake vs. shutdown |
| Power action rate limit | Per-user sliding window, 2/120 seconds | Confirmed wake and shutdown |
| Fixed remote operations | No Discord argument reaches an SSH command | MikroTik, NAS, edge actions |
| Configuration validation | Safe host, SSH username, script path, MAC, RouterOS interface, and Discord ID validation | Startup configuration |
| No local-shell interpolation | `subprocess.run()` is given argv lists | SSH and `ping` subprocesses |
| SSH process timeouts | 15, 60, and 180-second limits by action | Remote operation availability |
| Event-loop protection | Blocking SSH/ICMP runs through `asyncio.to_thread()` | Manual operations and watchdog ticks |
| Watchdog transition handling | State changes only after notification delivery | Network down/recovery alerts |
| Bootstrap secret files | Docker secrets for Infisical Universal Auth | Startup credential handling |
| No exposed application port | Compose contains no `ports` mapping | Container network exposure |

Read-only mounting limits in-container modification of the SSH directory but does not stop a
compromised container from reading or using keys.

## 4. Findings

| ID | Severity | Finding | Status |
|---|---|---|---|
| SEC-01 | Medium | SSH host identity is not pinned between connections | Open |
| SEC-02 | Medium | Container mounts the entire VPS SSH directory | Open |
| SEC-03 | Medium | Application container runs as root | Open |
| ABUSE-01 | Low | Only confirmed power actions have rate limits | Open |
| REL-01 | Low | Process-local state resets on restart | Partially mitigated (network + NAS watchdogs); accepted/documented for the rest |
| SUP-01 | Low | `infisicalsdk` dependency is not version-pinned | Open |
| MON-01 | Low | Some tunnel/path failures remain indistinguishable from a home outage | Open |
| CFG-01 | Informational | Application values refresh only on container restart | Accepted/documented |
| OPS-01 | Informational | Live Discord/VPS/network controls are not repository-verifiable | Verify in production |

### SEC-01: SSH host identity is not pinned

MikroTik, NAS, and edge SSH clients use:

```text
StrictHostKeyChecking=accept-new
UserKnownHostsFile=/dev/null
```

Because accepted keys are discarded, a later connection has no stored identity to compare. A changed
host key is effectively accepted as a new key. A private routed path reduces exposure but does not
replace SSH endpoint authentication.

Recommended action: create a managed `known_hosts` file containing pinned MikroTik, NAS, and edge
host keys; mount it read-only; use strict host-key checking. Test host-key rotation deliberately.

### SEC-02: Entire VPS SSH directory is exposed

Compose mounts `~/.ssh` to `/root/.ssh:ro`. This can expose unrelated private keys, SSH config, and
known-host data from the VPS user to the container.

Recommended action: use a dedicated non-root service identity and a dedicated SSH key authorized
only for Commander operations. Mount only that key and its pinned `known_hosts` file read-only.

### SEC-03: Container runs as root

The Dockerfile does not declare a non-root `USER`, and the mounted SSH directory is placed at
`/root/.ssh`. Root in the container does not automatically equal root on the VPS, but it broadens the
impact of an application/container compromise and encourages broad secret mounting.

Recommended action: create a non-root application user in the image, use a dedicated read-only key
directory owned by that user, and verify the required files/scripts work under that account.

### ABUSE-01: Rate limiting applies only to power actions

`RateLimiter` currently governs confirmed `wake` and `shutdown` operations. An authorized operator
can repeatedly invoke status checks or edge-info execution, potentially consuming SSH/device capacity
or creating noisy public output. The control-room/user gate limits who can do this, but does not
bound the rate.

Recommended action: apply shared per-user limits to status and edge operations, ideally in the common
operation layer so slash commands and panel buttons cannot diverge.

### REL-01: State is memory-only

Rate-limit windows, power-lock state, and active confirmation views still disappear on restart.

The network watchdog's down flag/down-since timestamp and the NAS watchdog's last-reminder timestamp
are no longer purely memory-only: both `NetworkWatchdog` and `NasUptimeWatchdog` now restore their
state from Turso Cloud's `event_history` table and persist it there through a shared, dedicated
`TursoProdWriter`, after the corresponding Discord notification has already succeeded
(`commander/netmonitor.py`, `commander/nasmonitor.py`, `commander/turso/writer.py`). This closes the
originally-described failure modes -- a duplicate DOWN alert with understated downtime after a
restart during a real network outage; a completely silent missed outage if the network recovered
during the restart window; a duplicate NAS reminder sent sooner than `NAS_MAX_AGE_REMINDER_MINUTE`
actually allows -- but only on a best-effort basis. Persisting depends on Turso Cloud being reachable
at that exact moment, which is a weaker guarantee than the in-memory Discord-notification path each
sits behind. A failed persist is retried on later watchdog ticks rather than dropped, but if the
container is rebuilt again before a retry succeeds, restore falls back to the last successfully
persisted row -- i.e. this narrows the original gap rather than eliminating it. The NAS restore has
an extra correctness requirement the network one does not: a restored reminder timestamp is only
trusted if it is not older than the NAS's live boot epoch (otherwise the NAS rebooted since that
reminder, and the timestamp is stale). See `machine_lore/ARCHITECTURE.md` §8,
`migrations/network_watchdog_state_migration.md`, and
`migrations/nas_uptime_watchdog_state_migration.md` for the full design, including the explicit
decision to keep `TursoProdWriter` on its own connection and executor so a write-path failure can
never affect `TursoCacheManager`'s configuration reads.

Recommended action: accept the remaining memory-only state (rate limits, power lock, confirmation
views) as small-control-plane behaviour. Do not persist power locks without a recovery/lease design.

### SUP-01: Infisical SDK dependency is unpinned

`requirements.txt` pins `discord.py==2.5.1` but lists `infisicalsdk` without a version. Future image
builds can receive a new SDK release even when repository code has not changed.

Recommended action: pin a reviewed compatible `infisicalsdk` version, rebuild, and keep a deliberate
dependency-update process.

### MON-01: Network monitor observes path reachability, not every failure cause

The watchdog probes `MIKROTIK_HOST` from the VPS. A healthy `NETWORK_ANCHOR_HOST` avoids declaring a
home outage when the VPS has lost general connectivity, but a private tunnel failure can look exactly
like a home-side gateway outage. Conversely, if the anchor is unavailable, the monitor intentionally
leaves state unchanged and sends no alert.

Recommended action: if cause classification matters, export a WireGuard handshake/peer-health signal
or an independent home probe and include it in the alert decision. Preserve the current transition
semantics so alerts are not silently dropped.

### CFG-01: Some configuration needs a restart to take effect

This finding is narrower than before the Turso migration. Discord identity and MikroTik
host/port/username are still materialized once in `load_config()` before the bot starts, so
changing an Infisical value in either group still requires a container restart. Edge, NAS, and
home-network operational values (including the shared SSH key path) instead live in Turso and are
re-read fresh from the local replica on every command and every watchdog tick
(`commander/turso/cache_manager.py`, `machine_lore/ARCHITECTURE.md` §6); an edit there applies on
the next action once periodic sync has pulled it in, with no restart. The Turso connection/sync
settings themselves (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `TURSO_DB_SYNC_INTERVAL`,
`TURSO_DB_DOWN_REMINDER`) are Infisical values read once at startup and still require a restart to
change. This is documented behaviour, not a secret-refresh failure.

### OPS-01: Production controls need external verification

This repository cannot prove Discord roles, bot installation scope, VPS firewall posture, SSH daemon
configuration, private routing, MikroTik permissions, NAS `sudoers`, or Infisical policies.

Recommended action: complete and retain the verification checklist below after material changes.

## 5. Threat and mitigation matrix

| Threat | Current mitigation | Residual risk |
|---|---|---|
| Random Discord user invokes an action | Guild, control-room, and user-ID checks | Compromised allowed account remains trusted |
| User invokes bot in an unintended Discord channel | Exact control-room channel gate | Channel-ID configuration must be correct |
| Destructive click by another operator | Requester-bound 60-second confirmation | Original requester may still be compromised |
| Overlapping power actions | One shared power lock | Lock state is reset by process restart |
| Command injection through Discord arguments | No arbitrary command arguments; fixed handlers | Infisical writers remain trusted configuration authors |
| Unsafe configuration tokens | Syntax validation for IDs, hosts, users, scripts, MAC, interface | Validation cannot guarantee target ownership or script safety |
| SSH interception | Private/routed network path | Host key is not persistently verified |
| Hang in remote command | Per-operation subprocess timeout | Failed/slow remote system still delays that operation |
| Event-loop blocking | Worker threads for SSH/ICMP | Thread-pool exhaustion remains theoretically possible under abuse |
| NAS left online | Uptime threshold and repeated reminders | Reminder state best-effort persisted to Turso (REL-01); still resets if Turso Cloud is unreachable at persist time |
| Home outage missed/noisy | Confirmed transition monitor and optional anchor | Tunnel-only fault classification is limited |
| Discord watchdog message pings a broad audience | `AllowedMentions.none()` | Channel audience/roles are externally managed |
| Image dependency drift | Python dependency manifest | Unpinned Infisical SDK can change at build time |
| Public inbound application attack | No Compose port mapping; outbound Discord connection | VPS/Docker host exposure remains external |

## 6. Production verification checklist

### Discord

- The bot is installed only in intended guilds with `bot` and `applications.commands` scopes.
- `DISCORD_GUILD_IDS`, control room, watchdog channel, and allowed user IDs are accurate.
- Control room permissions restrict visibility to the intended operators.
- The watchdog channel is distinct and has the intended notification audience.
- The bot has only needed channel permissions: View Channel, Send Messages, Embed Links, Attach Files
  where manual outputs are expected.
- Bot token rotation/revocation process exists and tokens never appear in logs or Compose files.

### VPS and container

- The container publishes no ports and runs the expected image/commit.
- Only required VPS management and private-routing ports are reachable externally.
- Infisical bootstrap files are readable only by the deployment identity/Docker as intended.
- A dedicated least-privilege SSH identity and pinned `known_hosts` file replace the broad mount.
- Container logs contain no token, key, or full sensitive script output unexpectedly.
- Docker image base and Python dependencies are updated on a deliberate cadence.

### Private network, MikroTik, NAS, and edge host

- Private routes cover only required subnets and home management interfaces are not public.
- MikroTik automation account can perform only the required ping/WoL operations.
- NAS SSH account uses key authentication and `sudoers` permits only the fixed shutdown script.
- NAS uptime and edge-info scripts need no elevated privilege and their paths/parents are not writable
  by automation users.
- SSH host keys for all targets are pinned and rotation has a documented procedure.

### Infisical and operational checks

- Universal Auth is scoped to the minimum project, environment, and secret path.
- Configuration changes have review/rotation/revocation procedures.
- After deployment, run the focused VPS smoke test in
  [WORKFLOW.md](./WORKFLOW.md#11-vps-deployment-and-smoke-test).
- Test watchdog routing under a planned condition, not by relying on a real production outage.

## 7. Recommended priority

1. Pin SSH host keys and mount only a dedicated least-privilege identity.
2. Run the application as a non-root user after changing the SSH mount accordingly.
3. Add shared rate limits for status and edge-info paths.
4. Pin `infisicalsdk` and adopt a deliberate dependency update/rebuild workflow.
5. Add a tunnel-health signal if the difference between tunnel failure and home outage matters.
6. Re-run this review and retain production-verification evidence after the hardening changes.

## 8. Verdict

The repository implements a small, gated Discord control plane with a useful separation between
manual control-room work and automatic watchdog notifications. Its interaction authorization,
fixed-action design, confirmation flow, and power-operation mutual exclusion are solid baseline
controls for a private automation service.

Its remaining exposure is concentrated in SSH/deployment hardening and intentionally memory-only
state. Treat the open findings as required operational follow-up, not as behaviour hidden by the
Discord interface.


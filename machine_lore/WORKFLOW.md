# Discord Commander Workflow

> Last reviewed: 2026-08-08
>
> Scope: application startup, Discord routing, panel controls, device operations, watchdogs,
> configuration, and deployment.

This document describes what the repository currently does. It does not make claims about the live
VPS, Discord server permissions, WireGuard routes, MikroTik, NAS, edge host, or Infisical policy.
Those boundaries are mapped in [ARCHITECTURE.md](./ARCHITECTURE.md); repository-visible security and
reliability gaps are in [AUDIT.md](./AUDIT.md).

## 1. Runtime overview

One process has three sources of work:

1. Guild-scoped Discord slash commands and control-panel button interactions.
2. A background watchdog for home-network reachability.
3. A background watchdog for NAS uptime.

All blocking network and SSH work is moved to a worker thread with `asyncio.to_thread()`. The
Discord event loop remains responsible for interactions, embeds, and watchdog scheduling.

```text
main()
  -> load_config() from Infisical
  -> CommanderBot(config)
       -> build authorization, controllers, state, limiter, and watchdogs
       -> register guild slash-command groups and persistent panel view
  -> Bot.run()
       -> setup_hook(): validate configured Discord channels; sync commands to every allowed guild
       -> on_ready(): start network and NAS-uptime watchdog tasks
       -> Discord gateway: slash commands and button interactions
```

Configuration is read once when the process starts. An Infisical change therefore requires a
container restart before the running controllers use it.

## 2. Startup, command registration, and shutdown

`commander.bot.main()` loads immutable configuration before attempting a Discord connection. Missing
or invalid Infisical values stop startup without logging their secret values.

`CommanderBot` then creates the following process-local components:

- `InteractionAuthorizer` for all slash commands and component clicks;
- `MikroTikClient`, `NasController`, and `EdgeController` for fixed SSH operations;
- `NetworkChecker` for direct container ICMP probes;
- `PowerOperationState` to make wake and shutdown mutually exclusive;
- `RateLimiter` for confirmed power actions;
- `NetworkWatchdog` and `NasUptimeWatchdog` with a shared Discord notifier.

During `setup_hook()` the bot registers a persistent `CommanderPanel`, validates both configured
channels through Discord, and synchronizes every application command to each ID in
`DISCORD_GUILD_IDS`. Guild-scoped commands update quickly and are intentionally not installed
globally.

Channel validation fails startup if either channel cannot be fetched, lies outside the allowed guild
set, cannot receive messages, or if control room and watchdog channel use the same ID.

`on_ready()` starts each watchdog once. `close()` cancels and awaits both tasks before closing the
Discord client. A reconnect does not create duplicate watchdog tasks.

## 3. Discord entry points

### 3.1 Slash commands

All slash commands are guild-scoped. Each command first applies the same authorization policy, then
delegates to a shared `OperatorOperations` method.

| Command | Action | Output destination |
|---|---|---|
| `/start` | Posts a short welcome plus the control panel. | Control room |
| `/panel` | Posts a new interactive control panel. | Control room |
| `/help` | Shows the supported command list. | Control room |
| `/ping` | Replies `Pong!`. | Control room |
| `/status nas` | Checks NAS reachability through MikroTik. | Control room |
| `/status net` | Probes the home gateway from the container. | Control room |
| `/edge info` | Runs the fixed edge information script. | Control room |
| `/wake nas` | Opens a requester-bound wake confirmation. | Control room |
| `/shutdown nas` | Opens a requester-bound shutdown confirmation. | Control room |

The commands are grouped in Discord as `status`, `edge`, `wake`, and `shutdown` where applicable.
There is no text-command parser and no message-content listener.

### 3.2 Persistent control panel

`/start` and `/panel` create the same `CommanderPanel` with static component IDs. The bot registers
one matching view at startup, allowing a previously sent panel to work after a container restart.

| Row | Buttons | Behaviour |
|---|---|---|
| 1 | Status Network, Edge Info, Status NAS | Runs the corresponding non-destructive operation. |
| 2 | Wake NAS, Shutdown NAS | Opens a confirmation panel; no power operation starts on the first click. |
| 3 | Help, Ping | Shows command help or a health reply. |

Button callbacks use the exact same operation methods as slash commands, so button and command
responses have the same output formatting and control-room destination.

### 3.3 Power confirmation buttons

Wake and shutdown are intentionally two-step actions:

1. An authorized operator invokes `/wake nas`, `/shutdown nas`, or the matching panel button.
2. Commander posts a public confirmation embed with **Confirm** and **Cancel**.
3. Only the operator who created that confirmation can click either button. The click also repeats
   the normal guild/channel/user authorization check.
4. Confirm begins the action; Cancel replaces the prompt with a no-action result.

The confirmation view expires after 60 seconds. On expiry its buttons are disabled when the original
message can still be edited. These temporary confirmation buttons are not persistent across a restart.

## 4. Authorization and response visibility

An interaction is accepted only when every condition below is true:

- it is not a direct message;
- its guild ID is present in `DISCORD_GUILD_IDS`;
- its channel ID equals `DISCORD_CONTROL_ROOM_CHANNEL_ID`; and
- its user ID is present in `DISCORD_ALLOWED_USER_IDS`.

Rejected interactions are logged with a reason and receive an ephemeral English denial. This means
the watchdog channel cannot be used as a control surface, even by an allowed user.

Accepted manual results are public in the control room (`ephemeral=False`) so operators can see the
operation history. Unhandled application-command errors are logged and receive an ephemeral generic
response. The watchdog channel receives no manual messages.

`DISCORD_WATCHDOG_CHANNEL_ID` is used exclusively by background watchdog notifications. Its alerts
contain no interactive buttons and mention nobody (`AllowedMentions.none()`).

## 5. Rate limiting and concurrency state

### 5.1 Rate limiting

`RateLimiter` is an in-memory, per-user sliding window. The current implementation applies it to a
confirmed power action only; status, edge-info, help, and ping are not rate limited.

| Operation | Limit | When a call is counted |
|---|---|---|
| Wake NAS | 2 confirmations per 120 seconds per user | After Confirm is clicked |
| Shutdown NAS | 2 confirmations per 120 seconds per user | After Confirm is clicked |

The state disappears on container restart. A user hitting a limit receives a public control-room
reply with the retry delay. The current scope is documentation of the code, not a claim that all
operations are uniformly throttled.

### 5.2 Power-operation lock

`PowerOperationState` keeps one active operation name behind an `asyncio.Lock`. Wake and shutdown
cannot overlap: a second confirmation while either operation is running receives a message asking
the operator to wait. The lock is released in a `finally` block after success, failure, or an
unexpected exception.

The NAS uptime watchdog skips its tick while a wake or shutdown is active. The network watchdog does
not skip because home-network reachability is independent of NAS power state.

## 6. Device workflows

### 6.1 NAS status

```text
slash command or panel button
  -> defer Discord response
  -> worker thread: NasController.is_online()
  -> worker thread: MikroTikClient.ping(NAS_IP)
  -> SSH to MikroTik
  -> RouterOS: /ping address=<NAS_IP> count=1
  -> green online, red offline, or unavailable embed
```

The status indicates the result of the MikroTik reachability check, not a full NAS health check.

### 6.2 Manual home-network status

```text
slash command or panel button
  -> defer Discord response
  -> worker thread: local ping -c <manual-count> -i 1 -W <timeout> MIKROTIK_HOST
  -> green reachable or red unreachable embed
```

The default manual count is `NETWORK_RECOVERY_PING_COUNT` (default `5`). A manual network check does
not change watchdog state. If the watchdog already considers the network down, the result embeds how
long that down state has been tracked.

The image installs `iputils-ping`; lack of that binary, a ping timeout, or an unexpected subprocess
failure is treated as unreachable by the low-level checker and logged.

### 6.3 Edge information

```text
slash command or panel button
  -> defer Discord response
  -> worker thread: SSH EDGE_SSH_USER@EDGE_INTERNAL_IP EDGE_INFO_SCRIPT
  -> return output as a code block or file attachment
```

`EDGE_INFO_SCRIPT` is a fixed, validated absolute path from Infisical. A successful output longer
than 3,200 characters is attached as `edge-info.txt`; command errors are shortened for an inline
error embed.

### 6.4 Wake NAS

After the two-step confirmation and power lock:

1. Commander first asks MikroTik whether the NAS is already reachable. If it is, Commander reports
   **NAS already online** and does not send a Wake-on-LAN packet.
2. Otherwise, Commander SSHes to MikroTik and runs the fixed RouterOS Wake-on-LAN operation using
   `NAS_MAC` and `NAS_WOL_INTERFACE`.
3. If the WoL command succeeds, Commander reports progress in the same interaction response.
4. It waits five seconds, then performs a MikroTik-mediated NAS status check.
5. It repeats for at most 24 attempts (roughly two minutes after the packet was sent).
6. It reports success with approximate elapsed boot time, or a timeout if NAS did not answer.

WoL only requests wake-up; a successful RouterOS command is not proof that the NAS later booted.

### 6.5 Shutdown NAS

After the two-step confirmation and power lock:

1. Commander SSHes directly to `NAS_IP` as `NAS_USER`.
2. The remote command is the fixed `sudo NAS_SHUTDOWN_SCRIPT`.
3. If the command succeeds, Commander waits five seconds, then checks NAS reachability through
   MikroTik. It repeats for at most 24 attempts (roughly two minutes after the command completes).
4. Commander reports **NAS offline** only when the NAS stops responding to the MikroTik check. If it
   remains reachable, Commander reports that shutdown was not confirmed.
5. Commander returns the script output in an embed, or attaches it as `nas-shutdown.txt` when it is
   longer than 3,000 characters, then releases the power lock.

The offline result confirms loss of reachability from MikroTik; it is not a hardware power-sensor
reading. The shutdown script remains the authoritative implementation of a graceful shutdown.

## 7. Automatic watchdogs

### 7.1 Home-network watchdog

This watchdog asks whether the home gateway is reachable from the VPS/container, independent of NAS
state. It is edge-triggered: it sends only on transition into DOWN and on transition back to
RECOVERED.

```text
state UP
  -> ping MIKROTIK_HOST with NETWORK_PING_COUNT probes
  -> reply received: remain silent
  -> no reply: optionally check NETWORK_ANCHOR_HOST
       -> anchor unavailable: log inconclusive, remain UP and send no alert
       -> anchor available: wait NETWORK_CONFIRM_DELAY and retry the check
            -> gateway replies: flap; remain UP and send no alert
            -> still unreachable: send DOWN alert to watchdog channel; set state DOWN

state DOWN
  -> ping MIKROTIK_HOST with NETWORK_RECOVERY_PING_COUNT probes
  -> no reply: remain silent
  -> reply received: send RECOVERED alert with measured downtime; clear DOWN state
```

The state changes only after Discord notification delivery succeeds. A temporary Discord send error
therefore leaves the prior state intact and permits the next tick to retry rather than silently
losing an alert. The state itself is memory-only and resets on container restart.

The alert embeds show the target host, verification details, and recovery downtime. `NETWORK_ANCHOR_HOST`
is an optional external control probe: it avoids declaring the home network down when the VPS itself
has lost general connectivity, but it cannot distinguish every possible tunnel failure from a home
outage.

### 7.2 NAS uptime watchdog

This watchdog reminds operators about a NAS that was left powered on longer than expected.

```text
every NAS_MONITOR_INTERVAL seconds
  -> skip if wake/shutdown is active
  -> MikroTik check: NAS online?
       -> offline: clear last reminder timestamp
       -> online: SSH to NAS and run NAS_UPTIME_SCRIPT without sudo
            -> parse Unix boot epoch
            -> uptime below NAS_MAX_AGE_MINUTE: clear last reminder timestamp
            -> uptime at/above max age: send reminder if first or reminder interval elapsed
```

The uptime script must print exactly a Unix epoch integer for the NAS boot time. It is intentionally
run without `sudo`. The first reminder can arrive up to one monitor interval after the configured
maximum age; later reminders use `NAS_MAX_AGE_REMINDER_MINUTE` while the NAS remains online.

The notifier records a reminder only after sending succeeds. An offline NAS or an uptime below the
threshold resets the reminder cycle. As with other in-memory state, a container restart forgets the
last reminder timestamp; a still-online NAS already over its threshold can alert again after restart.

## 8. SSH execution and output rules

All subprocesses use argv lists rather than a local shell. Remote values are configuration-derived
and validated before startup.

| Target/action | Remote operation | SSH process timeout | Explicit identity |
|---|---|---:|---|
| MikroTik NAS check | `/ping address=<NAS_IP> count=1` | 15 seconds | Optional `SSH_KEY_PATH` |
| MikroTik WoL | `/tool wol interface="<NAS_WOL_INTERFACE>" mac=<NAS_MAC>` | 15 seconds | Optional `SSH_KEY_PATH` |
| NAS shutdown | `sudo <NAS_SHUTDOWN_SCRIPT>` | 180 seconds | Optional `SSH_KEY_PATH` |
| NAS uptime | `<NAS_UPTIME_SCRIPT>` (no sudo) | 15 seconds | Optional `SSH_KEY_PATH` |
| Edge information | `<EDGE_INFO_SCRIPT>` | 60 seconds | OpenSSH default identities |

All current SSH clients use `StrictHostKeyChecking=accept-new` with
`UserKnownHostsFile=/dev/null`; the consequences and required hardening are documented in
[AUDIT.md](./AUDIT.md#sec-01-ssh-host-identity-is-not-pinned).

## 9. Configuration reference

### 9.1 Bootstrap environment and Docker secrets

| Environment variable | Required | Default | Purpose |
|---|---|---|---|
| `INFISICAL_CLIENT_ID_FILE` | Yes | — | Docker-secret path for Universal Auth client ID. |
| `INFISICAL_CLIENT_SECRET_FILE` | Yes | — | Docker-secret path for Universal Auth client secret. |
| `INFISICAL_PROJECT_ID_FILE` | Yes | — | Docker-secret path for Infisical project ID. |
| `INFISICAL_ENVIRONMENT` | No | `prod` | Infisical environment slug. |
| `INFISICAL_SECRET_PATH` | No | `/` | Infisical secret path. |
| `LOG_LEVEL` | No | `INFO` | Python console log level. |

The Compose file mounts the three required bootstrap values from `~/secrets/infisical/` on the VPS.
Application configuration below is then read from Infisical, not from Compose variables.

### 9.2 Discord application values

| Secret | Required | Format/meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | Bot token. Never log or commit it. |
| `DISCORD_GUILD_IDS` | Yes | One or more positive guild snowflakes, comma-separated. |
| `DISCORD_CONTROL_ROOM_CHANNEL_ID` | Yes | Positive channel snowflake for all commands, buttons, and manual replies. |
| `DISCORD_WATCHDOG_CHANNEL_ID` | Yes | Positive channel snowflake for automated alerts only; must differ from control room. |
| `DISCORD_ALLOWED_USER_IDS` | Yes | One or more positive user snowflakes, comma-separated. |

### 9.3 Device and operation values

| Secret | Required/default | Consumer |
|---|---|---|
| `MIKROTIK_HOST` | Required safe hostname/IP | MikroTik SSH target and direct home-network probe target. |
| `MIKROTIK_PORT` | Optional; `22` | MikroTik SSH port. |
| `MIKROTIK_USERNAME` | Required safe SSH username | MikroTik SSH account. |
| `NAS_IP` | Required safe hostname/IP | NAS address for MikroTik status checks and direct NAS SSH. |
| `NAS_MAC` | Required colon-separated MAC | Wake-on-LAN target. |
| `NAS_WOL_INTERFACE` | Required safe RouterOS interface name | Interface passed to RouterOS WoL. Spaces and common interface punctuation are accepted; quotes/control characters are not. |
| `NAS_SSH_PORT` | Optional; `22` | NAS SSH port. |
| `NAS_USER` | Required safe SSH username | NAS SSH account. |
| `NAS_SHUTDOWN_SCRIPT` | Required safe absolute path | Fixed privileged script invoked by shutdown. |
| `NAS_UPTIME_SCRIPT` | Required safe absolute path | Fixed unprivileged script that prints boot epoch. |
| `EDGE_INTERNAL_IP` | Required safe hostname/IP | Edge host SSH target. |
| `EDGE_SSH_PORT` | Optional; `22` | Edge host SSH port. |
| `EDGE_SSH_USER` | Required safe SSH username | Edge host SSH account. |
| `EDGE_INFO_SCRIPT` | Required safe absolute path | Fixed edge information script. |
| `SSH_KEY_PATH` | Optional | Explicit SSH identity used by MikroTik and NAS operations. Edge SSH relies on OpenSSH's default identity selection. |

### 9.4 Watchdog and probe values

| Secret | Required/default | Unit and behaviour |
|---|---|---|
| `NAS_MONITOR_INTERVAL` | Optional; `60` | Positive seconds between NAS uptime ticks. |
| `NAS_MAX_AGE_MINUTE` | Required | Positive minutes before first NAS-online reminder. |
| `NAS_MAX_AGE_REMINDER_MINUTE` | Required | Positive minutes between NAS-online reminders. |
| `TIMED_OUT_INTERVAL` | Optional; `10` | Positive minutes between home-network watchdog ticks. |
| `NETWORK_PING_COUNT` | Optional; `10` | Positive probe count for DOWN detection and confirmation. |
| `NETWORK_RECOVERY_PING_COUNT` | Optional; `5` | Positive probe count for recovery and manual status checks. |
| `NETWORK_PING_TIMEOUT` | Optional; `3` | Positive seconds waited per ICMP probe. |
| `NETWORK_ANCHOR_HOST` | Optional; disabled when empty | Safe hostname/IP used as the VPS uplink control probe. |
| `NETWORK_CONFIRM_DELAY` | Optional; `15` | Non-negative seconds before a second outage check. `0` disables the delay/confirmation round. |

## 10. Logging and failure handling

The `commander` logger writes timestamped records to stdout at `LOG_LEVEL`. It logs startup summary,
application-command sync counts, watchdog configuration, expected operation failures, authorization
rejections, and unexpected exceptions. It is designed not to log Infisical secret values.

Expected SSH/ping failures are rendered as English Discord embeds or script-output attachments in
the control room. Watchdog tick failures are logged and do not terminate their background loop.

## 11. VPS deployment and smoke test

The authoritative runtime is the VPS container. Local bot execution is intentionally not required for
this repository. Build and smoke test on the VPS after code changes:

```bash
git pull
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 -f
```

After an Infisical-only change, use `docker compose restart commander` rather than rebuilding.

Smoke-test sequence:

1. Confirm the `commander` container stays running in `docker compose ps`.
2. Inspect startup logs for missing/invalid secret errors, successful command sync, and both watchdog
   startup lines.
3. Confirm the bot appears online in each intended Discord guild and slash commands are visible in
   the control room.
4. From an allowed user in the control room, use `/ping` and `/help`.
5. Use `/panel`; click Ping, Help, Status NAS, Status Network, and Edge Info as appropriate for the
   environment.
6. Confirm command/button results appear in the control room, while no manual reply appears in the
   watchdog channel.
7. Confirm an unapproved user, wrong channel, or DM gets a private denial and cannot execute an
   operation.
8. Confirm `/wake nas` and `/shutdown nas` show a requester-bound 60-second confirmation. Exercise
   actual power operations only in a safe maintenance window.
9. Verify an automatic watchdog alert is delivered to the dedicated watchdog channel under a planned
   test condition; do not use a production outage as the test procedure.

The Docker image contains `openssh-client` and `iputils-ping`. If network status unexpectedly fails
after an image rebuild, the focused diagnostic is `docker compose exec commander ping -c 1
<MIKROTIK_HOST>` from the VPS.


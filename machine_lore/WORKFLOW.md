# Discord Commander Workflow

> Last reviewed: 2026-08-14
>
> Scope: application startup, Discord routing, panel controls, device operations, watchdogs,
> configuration, and deployment, across both the `discord-commander` and `worker-pooling`
> containers.

This document describes what the repository currently does. It does not make claims about the live
VPS, Discord server permissions, WireGuard routes, MikroTik, NAS, edge host, or Infisical policy.
Those boundaries are mapped in [ARCHITECTURE.md](./ARCHITECTURE.md); repository-visible security and
reliability gaps are in [AUDIT.md](./AUDIT.md).

## 1. Runtime overview

Two independent processes, from the same image, each with their own entrypoint:

**Commander** (`commander.bot`, container `discord-commander`) has two sources of work:

1. Guild-scoped Discord slash commands and control-panel button interactions.
2. Its own background synchronization task for its own Turso-backed configuration replica.

**Worker Pooling** (`worker.main`, container `worker-pooling`) has three sources of work, none of
them triggered by a user:

1. A background watchdog for home-network reachability.
2. A background watchdog for NAS uptime.
3. A background synchronization task for its own Turso-backed configuration replica.

In both processes, all blocking network and SSH work is moved to a worker thread with
`asyncio.to_thread()`. Commander's event loop is responsible for interactions, embeds, and its
sync task's scheduling; Worker's event loop is responsible for the two watchdogs' scheduling and
its own sync task.

```text
commander.bot.main()
  -> load_turso_config() from Infisical (Turso URL/token/sync intervals)
  -> TursoCacheManager.bootstrap(): delete any existing local replica, pull a fresh one from
     Turso Cloud into Commander's own volume (fails startup on error/timeout)
  -> load_config(turso_cache): a one-time startup snapshot for Discord identity, MikroTik
     identity, and channel validation (edge/NAS/home-network values are NOT held from this
     snapshot -- see below)
  -> CommanderBot(config, turso_cache)
       -> build authorization, controllers, and state -- each controller is handed the
          `TursoCacheManager` (a `ConfigSource`) itself, not a config value
       -> register guild slash-command groups and persistent panel view
  -> Bot.run()
       -> setup_hook(): validate configured Discord channels; sync commands to every allowed guild
       -> on_ready(): start Commander's own Turso-sync task; send one startup notification
       -> Discord gateway: slash commands and button interactions
```

```text
worker.main.main()
  -> load_turso_config() from Infisical (Turso URL/token/sync intervals)
  -> TursoCacheManager.bootstrap(): delete any existing local replica, pull a fresh one from
     Turso Cloud into Worker's own volume (fails startup on error/timeout)
  -> TursoProdWriter(turso_config): lazy-connect write path for event_history, constructed only
     here -- Commander has none
  -> load_config(turso_cache): reused startup snapshot; Worker only actually needs
     config.discord.bot_token / watchdog_channel_id / network.watchdog_interval_seconds from it
  -> create_http_only_client(bot_token): logs in without opening a gateway session
  -> DiscordWatchdogNotifier(discord_client, ...); attach_notifier() onto this Worker's own
     TursoCacheManager (Commander's instance deliberately never gets one -- see §7.3)
  -> build MikroTikClient / NetworkChecker / NasController, then NetworkWatchdog and
     NasUptimeWatchdog
  -> three independent asyncio tasks: network-watchdog, nas-uptime-watchdog, turso-sync
  -> await SIGTERM/SIGINT
       -> cancel all three tasks, await them
       -> close the Turso cache manager, the Turso writer, and the Discord client
```

Discord identity (bot token, guild/channel/user IDs) and MikroTik host/port/username come from
Infisical and are read once at startup into an immutable `Config` snapshot **in both processes**,
so changing those still requires restarting both containers. Edge, NAS, and home-network values
(including the shared SSH key path) are the opposite: every command and every watchdog tick calls
`load_edge_config()` / `load_nas_config()` / `load_nas_watchdog_config()` / `load_network_config()`
/ `load_mikrotik_config()` (for the shared SSH key path) fresh against that process's own local
Turso replica file at the moment it runs. There is no in-memory copy of these values between
actions. A value edited in Turso Cloud applies on the *next* action in *each* process once that
process's own periodic sync (or its next restart's fresh bootstrap) has pulled it into its replica
file -- no restart is needed for these groups, but the two processes can briefly disagree with each
other between their independent sync ticks. If a value is missing or fails validation at the
moment an action reads it, that single action fails (Commander command: a Discord error reply via
the existing generic exception handling in `operations.py`; Worker watchdog tick: logged and
retried after a 60-second backoff) rather than crashing the process -- see
[§9.2a](#92a-turso-connection-and-sync-values-infisical) and [§7.3](#73-turso-synchronization-watchdog).

## 2. Startup, command registration, and shutdown

### 2.1 Commander

`commander.bot.main()` bootstraps Commander's own Turso local replica, then loads immutable
configuration, before attempting a Discord connection. Missing/invalid Infisical values, a failed
or timed-out Turso bootstrap, and invalid Turso-sourced values all stop startup without logging
secret values (hardcoded bootstrap timeout: `BOOTSTRAP_TIMEOUT_SECONDS = 30` in
`shared/turso/cache_manager.py`). The connect/pull call has no cooperative cancellation hook, so
the 30-second bound only stops *waiting* for it; the dedicated worker thread keeps running until
pyturso's call actually returns, and a late-arriving connection is closed and discarded rather than
adopted (see the docstring on `TursoCacheManager.bootstrap()`).

`CommanderBot` then creates the following process-local components:

- `InteractionAuthorizer` for all slash commands and component clicks;
- `MikroTikClient`, `NasController`, and `EdgeController` for fixed SSH operations -- each holds
  the `TursoCacheManager` itself and reads its config group fresh on every call, not a config value;
- `NetworkChecker` for direct container ICMP probes, same live-read pattern;
- `PowerOperationState` to make wake and shutdown mutually exclusive (Commander-local only; Worker
  does not consult it -- see [ARCHITECTURE.md §8.4](./ARCHITECTURE.md#84-no-cross-process-wakeshutdown-coordination-by-design));
- `RateLimiter` for confirmed power actions.

During `setup_hook()` the bot registers a persistent `CommanderPanel`, validates both configured
channels through Discord, and synchronizes every application command to each ID in
`DISCORD_GUILD_IDS`. Guild-scoped commands update quickly and are intentionally not installed
globally.

Channel validation fails startup if either channel cannot be fetched, lies outside the allowed guild
set, cannot receive messages, or if control room and watchdog channel use the same ID.

`on_ready()` starts Commander's own Turso-sync task and sends one **Commander online** notification
to the watchdog channel for the process lifetime. The in-memory startup flag prevents Discord
reconnects from sending duplicate startup notifications; a new container/process has fresh memory
and sends a new notification after it connects. `close()` cancels and awaits the sync task before
closing the local Turso cache and the Discord client.

### 2.2 Worker Pooling

`worker.main.main()` bootstraps Worker's own Turso local replica (a separate volume from
Commander's -- see [ARCHITECTURE.md §6](./ARCHITECTURE.md#6-configuration-and-secret-ownership)),
constructs a `TursoProdWriter` (Worker is the only process that ever writes `event_history`), loads
the same `Config` snapshot Commander does (reusing `load_config()` even though Worker only needs a
few fields off it, to avoid a second config-loading code path), logs a Discord client in via
`shared.discord_client.create_http_only_client()` (HTTP only -- no gateway session, since Worker
never needs to receive anything from Discord), and attaches a `DiscordWatchdogNotifier` to its own
`TursoCacheManager` so Turso DOWN/RECOVERED alerts are sent from Worker (Commander's own cache
manager never gets a notifier attached, so the alert isn't sent twice -- see §7.3).

It then starts three independent `asyncio` tasks -- `network-watchdog`, `nas-uptime-watchdog`,
`turso-sync` -- and waits on a `SIGTERM`/`SIGINT`-triggered `asyncio.Event`. On shutdown it cancels
and awaits all three tasks (one task's failure doesn't stop the others, since each is wrapped in its
own try/except inside its `run()` loop -- see §7), then closes the Turso cache manager, the Turso
writer, and the Discord client in that order.

Worker registers no Discord commands and has no `setup_hook()`/channel-validation step of its own;
it only ever sends to the already-known watchdog channel ID from its `Config` snapshot.

## 3. Discord entry points

### 3.1 Slash commands

All slash commands are guild-scoped and handled entirely by Commander. Each command first applies
the same authorization policy, then delegates to a shared `OperatorOperations` method.

| Command | Action | Output destination |
|---|---|---|
| `/start` | Posts a short welcome plus the control panel. | Control room |
| `/panel` | Posts a new interactive control panel. | Control room |
| `/help` | Shows the supported command list. | Control room |
| `/ping` | Replies `Pong!`. | Control room |
| `/status nas` | Checks NAS reachability through MikroTik. | Control room |
| `/status net` | Probes the home gateway from the container; also shows Worker's last-persisted network-watchdog transition, read from Commander's own synced Turso replica. | Control room |
| `/edge info` | Runs the fixed edge information script. | Control room |
| `/wake nas` | Opens a requester-bound wake confirmation. | Control room |
| `/shutdown nas` | Opens a requester-bound shutdown confirmation. | Control room |

The commands are grouped in Discord as `status`, `edge`, `wake`, and `shutdown` where applicable.
There is no text-command parser and no message-content listener. Worker registers no commands.

### 3.2 Persistent control panel

`/start` and `/panel` create the same `CommanderPanel` with static component IDs. Commander
registers one matching view at startup, allowing a previously sent panel to work after a Commander
restart.

| Row | Buttons | Behaviour |
|---|---|---|
| 1 | Status Network, Edge Info, Status NAS | Runs the corresponding non-destructive operation. |
| 2 | Wake NAS, Shutdown NAS | Opens a confirmation panel; no power operation starts on the first click. |
| 3 | Help, Ping | Shows command help or a health reply. |

Button callbacks use the exact same operation methods as slash commands, so button and command
responses have the same output formatting and control-room destination.

### 3.3 Power confirmation buttons

Wake and shutdown are intentionally two-step actions, entirely within Commander:

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
the watchdog channel cannot be used as a control surface, even by an allowed user -- and Worker
doesn't listen for interactions at all, so there's nothing for it to authorize.

Accepted manual results are public in the control room (`ephemeral=False`) so operators can see the
operation history. Unhandled application-command errors are logged and receive an ephemeral generic
response. The watchdog channel receives no manual messages from either process.

`DISCORD_WATCHDOG_CHANNEL_ID` is used exclusively by automatic lifecycle and watchdog
notifications, from both containers. Its alerts contain no interactive buttons and mention nobody
(`AllowedMentions.none()`).

## 5. Rate limiting and concurrency state

### 5.1 Rate limiting

`RateLimiter` is an in-memory, per-user sliding window, owned by Commander. The current
implementation applies it to a confirmed power action only; status, edge-info, help, and ping are
not rate limited.

| Operation | Limit | When a call is counted |
|---|---|---|
| Wake NAS | 2 confirmations per 120 seconds per user | After Confirm is clicked |
| Shutdown NAS | 2 confirmations per 120 seconds per user | After Confirm is clicked |

The state disappears on a Commander restart. A user hitting a limit receives a public control-room
reply with the retry delay. The current scope is documentation of the code, not a claim that all
operations are uniformly throttled.

### 5.2 Power-operation lock

`PowerOperationState`, owned by Commander, keeps one active operation name behind an `asyncio.Lock`.
Wake and shutdown cannot overlap: a second confirmation while either operation is running receives
a message asking the operator to wait. The lock is released in a `finally` block after success,
failure, or an unexpected exception.

This lock is process-local to Commander and has no counterpart in Worker. The NAS uptime watchdog
(now in Worker) does not consult it -- its ordinary online/uptime gating already covers the
wake/shutdown window on its own, without needing any cross-process signal; see
[ARCHITECTURE.md §8.4](./ARCHITECTURE.md#84-no-cross-process-wakeshutdown-coordination-by-design).
The network watchdog never skipped for this reason either, since home-network reachability is
independent of NAS power state.

## 6. Device workflows

All of the following run in Commander; none of them run in Worker.

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
  -> read the latest home_network event_history row from Commander's own synced Turso replica;
     if Worker's watchdog currently considers the network DOWN, add how long
```

The default manual count is `NETWORK_RECOVERY_PING_COUNT` (default `5`). A manual network check does
not change watchdog state (that state belongs entirely to Worker). If Worker's watchdog already
considers the network down, the result embeds how long that down state has been tracked, based on
whatever Commander's own replica has synced in -- this can lag Worker's live state by up to
Commander's `TURSO_DB_SYNC_INTERVAL`.

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
This entire flow, including its polling, runs inside the single Discord interaction in Commander --
it is never handed off to Worker (see [ARCHITECTURE.md §1](./ARCHITECTURE.md#1-system-context)).

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
reading. The shutdown script remains the authoritative implementation of a graceful shutdown. Like
Wake, this whole flow (with its polling) stays in Commander.

## 7. Automatic watchdogs

All three watchdogs below run in Worker (`worker/`); none run in Commander.

### 7.1 Home-network watchdog

This watchdog (`worker.network_watchdog.NetworkWatchdog`) asks whether the home gateway is
reachable from the VPS/container, independent of NAS state. It is edge-triggered: it sends only on
transition into DOWN and on transition back to RECOVERED.

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

The state changes only after Discord notification delivery succeeds (via Worker's HTTP-only Discord
client). A temporary Discord send error therefore leaves the prior state intact and permits the
next tick to retry rather than silently losing an alert. The state itself is memory-only within
Worker and resets on a Worker restart (subject to the Turso restore in
[ARCHITECTURE.md §8.3](./ARCHITECTURE.md#83-watchdog-state-restore-worker)).

The alert embeds show the target host, verification details, and recovery downtime. `NETWORK_ANCHOR_HOST`
is an optional external control probe: it avoids declaring the home network down when the VPS itself
has lost general connectivity, but it cannot distinguish every possible tunnel failure from a home
outage.

### 7.2 NAS uptime watchdog

This watchdog (`worker.nas_reminder.NasUptimeWatchdog`) reminds operators about a NAS that was left
powered on longer than expected.

```text
every NAS_MONITOR_INTERVAL seconds
  -> MikroTik check: NAS online?
       -> offline: clear last reminder timestamp
       -> online: SSH to NAS and run NAS_UPTIME_SCRIPT without sudo
            -> parse Unix boot epoch
            -> uptime below NAS_MAX_AGE_MINUTE: clear last reminder timestamp
            -> uptime at/above max age: send reminder if first or reminder interval elapsed
```

There is no explicit "skip while wake/shutdown is active" branch (there was one before the
Commander/Worker split, tied to an in-memory lock that can no longer exist across processes) -- see
[ARCHITECTURE.md §8.4](./ARCHITECTURE.md#84-no-cross-process-wakeshutdown-coordination-by-design)
for why the ordinary online/uptime gating above already covers that window on its own.

The uptime script must print exactly a Unix epoch integer for the NAS boot time. It is intentionally
run without `sudo`. The first reminder can arrive up to one monitor interval after the configured
maximum age; later reminders use `NAS_MAX_AGE_REMINDER_MINUTE` while the NAS remains online.

The notifier records a reminder only after sending succeeds. An offline NAS or an uptime below the
threshold resets the reminder cycle. As with other in-memory state, a Worker restart forgets the
last reminder timestamp (subject to the Turso restore in
[ARCHITECTURE.md §8.3](./ARCHITECTURE.md#83-watchdog-state-restore-worker)); a still-online NAS
already over its threshold can alert again after restart if the restore doesn't apply.

### 7.3 Turso synchronization watchdog

This exists **in both processes independently** -- it is not a home-device probe, but a report on
the health of that process's own configuration replica (`shared/turso/cache_manager.py`), separate
from the network and NAS watchdogs above.

```text
every TURSO_DB_SYNC_INTERVAL minutes (per process)
  -> pull that process's own local replica from Turso Cloud
       -> success while UP: remain silent, log only
       -> success while DOWN: refresh local replica; state UP; RECOVERED alert pending
       -> failure while UP: state DOWN; record down_since; DOWN alert pending
       -> failure while DOWN, no alert pending: mark a reminder pending only if
          TURSO_DB_DOWN_REMINDER minutes have elapsed since the last *delivered*
          DOWN/reminder alert; otherwise stay silent
       -> any tick with a DOWN/reminder/RECOVERED alert pending: attempt delivery
          (Worker only -- see below)
```

Turso health (UP/DOWN) and notification-delivery state are tracked separately per process
(`shared/turso/cache_manager.py`): health always reflects that process's own actual sync result on
this tick, but a DOWN, reminder, or RECOVERED alert only clears its "pending" flag once the Discord
send actually succeeds. **Only Worker's `TursoCacheManager` has a notifier attached** -- Commander's
own instance tracks health/pending state identically but never has anything to send, so a Turso
Cloud outage (which affects both processes' sync at the same instant) produces exactly one alert,
not two. A failed send is logged and retried on the *next* sync tick with the same message (an
unsent initial DOWN alert is retried as DOWN, not silently escalated to a reminder); health itself
is never faked to make a notification retry happen, and a pending RECOVERED alert survives a failed
send without reverting the already-correct UP state.

A sync failure never stops either process and never deletes that process's local replica:
`read_group()` calls made by an in-flight action keep returning whatever that process's local
replica file currently holds (see §1). Each process's sync task exists to (a) keep its own on-disk
replica fresh so every subsequent `load_edge_config()` / `load_nas_config()` / `load_network_config()`
call in that process (and that container's *next* restart's fresh bootstrap) sees current data, and
(b) let Worker alert operators when Turso Cloud itself is unreachable. Since
`EdgeConfig`/`NasConfig`/`NetworkConfig` values are read fresh on every action rather than held in
`Config` after startup, a Turso Cloud edit applies to each process as soon as *that process's*
periodic sync has pulled it in -- no restart required, though the two processes can briefly
disagree between their independent sync ticks. Discord identity and MikroTik host/port/username
(Infisical) are the exception: those are still read once into `Config` at startup in both processes
and do require restarting both containers (`AUDIT.md` CFG-01).

## 8. SSH execution and output rules

All subprocesses use argv lists rather than a local shell. Remote values are configuration-derived
and validated before startup.

| Target/action | Remote operation | SSH process timeout | Explicit identity | Runs in |
|---|---|---:|---|---|
| MikroTik NAS check | `/ping address=<NAS_IP> count=1` | 15 seconds | Optional `SSH_KEY_PATH` | Commander, Worker |
| MikroTik WoL | `/tool wol interface="<NAS_WOL_INTERFACE>" mac=<NAS_MAC>` | 15 seconds | Optional `SSH_KEY_PATH` | Commander |
| NAS shutdown | `sudo <NAS_SHUTDOWN_SCRIPT>` | 180 seconds | Optional `SSH_KEY_PATH` | Commander |
| NAS uptime | `<NAS_UPTIME_SCRIPT>` (no sudo) | 15 seconds | Optional `SSH_KEY_PATH` | Worker |
| Edge information | `<EDGE_INFO_SCRIPT>` | 60 seconds | OpenSSH default identities | Commander |

The MikroTik NAS check runs from both containers: Commander for `/status nas`, Wake, and Shutdown;
Worker for both watchdogs' reachability gating.

All current SSH clients use `StrictHostKeyChecking=accept-new` with
`UserKnownHostsFile=/dev/null`; the consequences and required hardening are documented in
[AUDIT.md](./AUDIT.md#sec-01-ssh-host-identity-is-not-pinned).

## 9. Configuration reference

### 9.1 Bootstrap environment and Docker secrets

Both containers read the same set of bootstrap environment variables and Docker secrets.

| Environment variable | Required | Default | Purpose |
|---|---|---|---|
| `INFISICAL_CLIENT_ID_FILE` | Yes | — | Docker-secret path for Universal Auth client ID. |
| `INFISICAL_CLIENT_SECRET_FILE` | Yes | — | Docker-secret path for Universal Auth client secret. |
| `INFISICAL_PROJECT_ID_FILE` | Yes | — | Docker-secret path for Infisical project ID. |
| `INFISICAL_ENVIRONMENT` | No | `prod` | Infisical environment slug. |
| `INFISICAL_SECRET_PATH` | No | `/` | Infisical secret path. |
| `LOG_LEVEL` | No | `INFO` | Python console log level. |
| `TURSO_LOCAL_DB_PATH` | No | `/data/turso/local.db` | Path to that container's own local Turso replica file; deleted and rebuilt on every startup (§7.3). Mounted as the `commander_turso_data` Compose volume in the `commander` service, `worker_turso_data` in the `worker` service -- the two are never shared (see [ARCHITECTURE.md §6](./ARCHITECTURE.md#6-configuration-and-secret-ownership)). |

The Compose file mounts the three required bootstrap values from `~/secrets/infisical/` on the VPS,
into both services. Discord and MikroTik application configuration is read from Infisical by both
processes; edge/NAS/home-network application configuration is read from each process's own Turso
replica at `TURSO_LOCAL_DB_PATH` once its own `TursoCacheManager.bootstrap()` has populated it
(§9.2a, §9.3, §9.4) — none of it comes from Compose variables.

### 9.2 Discord application values

Read by both processes' `load_config()` at startup, though Worker only actually uses
`bot_token`/`watchdog_channel_id` out of this group.

| Secret | Required | Format/meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | Bot token. Never log or commit it. Commander uses it for a full gateway session; Worker uses it for an HTTP-only login (`shared.discord_client.create_http_only_client`). |
| `DISCORD_GUILD_IDS` | Yes | One or more positive guild snowflakes, comma-separated. Commander-only use (command sync/authorization). |
| `DISCORD_CONTROL_ROOM_CHANNEL_ID` | Yes | Positive channel snowflake for all commands, buttons, and manual replies. Commander-only use. |
| `DISCORD_WATCHDOG_CHANNEL_ID` | Yes | Positive channel snowflake for automated alerts only; must differ from control room. Used by both processes. |
| `DISCORD_ALLOWED_USER_IDS` | Yes | One or more positive user snowflakes, comma-separated. Commander-only use. |

### 9.2a Turso connection and sync values (Infisical)

These four values must exist before either process's own Turso replica does, so they stay on the
Infisical path rather than living inside the data they are used to fetch
(`shared/config.py:load_turso_config`). Read independently by both processes at startup.

| Secret | Required/default | Format/meaning |
|---|---|---|
| `TURSO_DATABASE_URL` | Yes | libSQL/Turso Cloud database URL (`remote_url` for `turso.sync.connect`). |
| `TURSO_AUTH_TOKEN` | Yes | Turso auth token. Never log or commit it. |
| `TURSO_DB_SYNC_INTERVAL` | Yes | Positive integer **minutes** between periodic replica syncs (§7.3), applied independently in each process. |
| `TURSO_DB_DOWN_REMINDER` | Yes | Positive integer **minutes** between DOWN reminder alerts while Turso stays unreachable (§7.3); only observable via Worker's alerts. |

### 9.3 Device and operation values

MikroTik identity stays on Infisical (it is the SSH target used to reach the rest of the home
network, not itself managed through Turso) and is read once at startup by both processes. Edge,
NAS, and home-network values come from each process's own Turso replica and are read fresh on every
action via `TursoCacheManager.read_group(identifier)` against the `master_configurations` table,
grouped by the `identifier` column shown below -- no restart needed for these to take effect once
that process has synced.

The `master_configurations` schema is owned in Turso Cloud, not by a migration in this repository
(there is no `.sql`/schema-migration file here). `read_group()` rejects a duplicate normalized
`(identifier, attribute_key)` pair as an application-level configuration error rather than silently
picking one row, but that only catches the problem at read time. The schema itself should carry a
matching database constraint so a duplicate is rejected at write time:

```sql
UNIQUE(identifier, attribute_key)
```

Apply this manually in Turso Cloud/Studio on `master_configurations` when convenient; it is a
recommendation to enforce there, not something this repository can migrate itself.

| Value | Source | Required/default | Consumer |
|---|---|---|---|
| `MIKROTIK_HOST` | Infisical | Required safe hostname/IP | MikroTik SSH target and direct home-network probe target. Commander and Worker. |
| `MIKROTIK_PORT` | Infisical | Optional; `22` | MikroTik SSH port. Commander and Worker. |
| `MIKROTIK_USERNAME` | Infisical | Required safe SSH username | MikroTik SSH account. Commander and Worker. |
| `NAS_IP` | Turso (`nas`) | Required safe hostname/IP | NAS address for MikroTik status checks and direct NAS SSH. Commander (status/wake/shutdown) and Worker (uptime reminder). |
| `NAS_MAC` | Turso (`nas`) | Required colon-separated MAC | Wake-on-LAN target. Commander only. |
| `NAS_WOL_INTERFACE` | Turso (`nas`) | Required safe RouterOS interface name | Interface passed to RouterOS WoL. Spaces and common interface punctuation are accepted; quotes/control characters are not. Commander only. |
| `NAS_SSH_PORT` | Turso (`nas`) | Optional; `22` | NAS SSH port. Commander and Worker. |
| `NAS_USER` | Turso (`nas`) | Required safe SSH username | NAS SSH account. Commander and Worker. |
| `NAS_SHUTDOWN_SCRIPT` | Turso (`nas`) | Required safe absolute path | Fixed privileged script invoked by shutdown. Commander only. |
| `NAS_UPTIME_SCRIPT` | Turso (`nas`) | Required safe absolute path | Fixed unprivileged script that prints boot epoch. Worker only. |
| `EDGE_INTERNAL_IP` | Turso (`edge`) | Required safe hostname/IP | Edge host SSH target. Commander only. |
| `EDGE_SSH_PORT` | Turso (`edge`) | Optional; `22` | Edge host SSH port. Commander only. |
| `EDGE_SSH_USER` | Turso (`edge`) | Required safe SSH username | Edge host SSH account. Commander only. |
| `EDGE_INFO_SCRIPT` | Turso (`edge`) | Required safe absolute path | Fixed edge information script. Commander only. |
| `SSH_KEY_PATH` | Turso (`home_network`) | Optional | Explicit SSH identity used by MikroTik and NAS operations, in both processes. Edge SSH relies on OpenSSH's default identity selection. |

### 9.4 Watchdog and probe values

All read from each process's own Turso replica.

| Value | Source | Required/default | Unit and behaviour | Consumer |
|---|---|---|---|---|
| `NAS_MONITOR_INTERVAL` | Turso (`nas`) | Optional; `60` | Positive seconds between NAS uptime ticks. | Worker only |
| `NAS_MAX_AGE_MINUTE` | Turso (`nas`) | Required | Positive minutes before first NAS-online reminder. | Worker only |
| `NAS_MAX_AGE_REMINDER_MINUTE` | Turso (`nas`) | Required | Positive minutes between NAS-online reminders. | Worker only |
| `TIMED_OUT_INTERVAL` | Turso (`home_network`) | Optional; `10` | Positive minutes between home-network watchdog ticks. | Worker only |
| `NETWORK_PING_COUNT` | Turso (`home_network`) | Optional; `10` | Positive probe count for DOWN detection and confirmation. | Worker only |
| `NETWORK_RECOVERY_PING_COUNT` | Turso (`home_network`) | Optional; `5` | Positive probe count for recovery checks and the default manual-status probe count. | Commander and Worker |
| `NETWORK_PING_TIMEOUT` | Turso (`home_network`) | Optional; `3` | Positive seconds waited per ICMP probe. | Commander and Worker |
| `NETWORK_ANCHOR_HOST` | Turso (`home_network`) | Optional; disabled when empty | Safe hostname/IP used as the VPS uplink control probe. | Worker only |
| `NETWORK_CONFIRM_DELAY` | Turso (`home_network`) | Optional; `15` | Non-negative seconds before a second outage check. `0` disables the delay/confirmation round. | Worker only |
| `ENABLE_STARTUP_NOTIFICATION` | Turso (`internal`) | Optional; `TRUE` | `TRUE`/`FALSE`. Whether Commander posts the "Commander online" message to the watchdog channel after each restart. | Commander only |

## 10. Logging and failure handling

Commander's process writes timestamped records to stdout at `LOG_LEVEL` under the `"commander"`
logger (plus module loggers like `commander.operations`, `shared.turso`, etc.); Worker's process
does the same under the `"worker"` logger (plus `worker.network_watchdog`,
`worker.nas_reminder`, `shared.turso`, etc.). Both log startup summary, watchdog/sync configuration
where applicable, expected operation failures, and unexpected exceptions. Commander additionally
logs application-command sync counts and authorization rejections. Neither is designed to log
Infisical secret values, and the Turso cache manager (shared by both) is designed not to log
`TURSO_AUTH_TOKEN` or any connection string containing it.

Expected SSH/ping failures in Commander are rendered as English Discord embeds or script-output
attachments in the control room. Watchdog tick failures in Worker are logged and do not terminate
their background loop -- each of the three tasks (`network-watchdog`, `nas-uptime-watchdog`,
`turso-sync`) wraps its own body in try/except, so one task's unexpected exception can't take down
another.

## 11. VPS deployment and smoke test

The authoritative runtime is the VPS, running both containers. Local execution is intentionally not
required for this repository. Build and smoke test on the VPS after code changes:

```bash
git pull
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 -f commander
docker compose logs --tail=100 -f worker
```

A Turso-row change to an edge/NAS/home-network value needs no restart: it applies to each process
once that process's own periodic sync (or a manual wait of up to `TURSO_DB_SYNC_INTERVAL`) has
pulled it into its replica (§1, §7.3) -- the two processes can briefly disagree between their
independent sync ticks. An Infisical-only change (Discord identity, MikroTik host/port/username, or
the Turso connection/sync values themselves) still needs `docker compose restart commander worker`
rather than a rebuild, since those are only read once at startup in both processes.

### 11.1 Commander smoke test

1. Confirm the `commander` container stays running in `docker compose ps`.
2. Inspect Commander's startup logs for: Turso local-database removal (only present on a restart
   with a prior replica), successful Turso bootstrap, missing/invalid secret errors, and successful
   command sync.
3. Confirm the bot appears online in each intended Discord guild and slash commands are visible in
   the control room.
4. From an allowed user in the control room, use `/ping` and `/help`.
5. Use `/panel`; click Ping, Help, Status NAS, Status Network, and Edge Info as appropriate for the
   environment.
6. Confirm command/button results appear in the control room, while no manual reply appears in the
   watchdog channel.
7. Confirm an unapproved user, wrong channel, or DM gets a private denial and cannot execute an
   operation.
8. Confirm `/wake nas` and `/shutdown nas` show a requester-bound 60-second confirmation, and that
   the whole flow (including polling until UP/offline) completes without ever touching the `worker`
   container. Exercise actual power operations only in a safe maintenance window.

### 11.2 Worker smoke test

1. Confirm the `worker` container stays running in `docker compose ps`.
2. Inspect Worker's startup logs for: successful Turso bootstrap, "Network watchdog started", "NAS
   uptime watchdog started", and no import/connection errors.
3. Verify an automatic network-watchdog alert is delivered to the dedicated watchdog channel under a
   planned test condition; do not use a production outage as the test procedure.
4. Verify a NAS-uptime reminder is delivered under a planned condition (leave a test NAS on past its
   configured threshold, or temporarily lower `NAS_MAX_AGE_MINUTE` in Turso Studio and confirm it
   applies without restarting `worker`).
5. Verify Turso DOWN/RECOVERED alerts under a planned condition (e.g. temporarily revoke the Turso
   auth token, wait for the next `TURSO_DB_SYNC_INTERVAL` tick, confirm exactly one DOWN alert from
   `worker`, restore the token, confirm exactly one RECOVERED alert on the next tick) — not by
   relying on an actual Turso Cloud outage, and confirm `commander`'s logs show the same sync
   failure/recovery without posting a second alert.
6. Verify live-reload for a Turso-backed watchdog value: edit a low-risk value (e.g.
   `NAS_MAX_AGE_REMINDER_MINUTE`) in Turso Studio, wait past the next `TURSO_DB_SYNC_INTERVAL` tick,
   and confirm the new behaviour is reflected **without restarting** `worker`.

### 11.3 Isolation smoke test

This is the main acceptance test for the two-container split
([ARCHITECTURE.md §8.1](./ARCHITECTURE.md#81-failure-isolation-between-containers)).

```bash
docker compose restart worker
```

Confirm `commander` stays connected and `/ping`, `/status nas`, `/status net` keep responding
throughout.

```bash
docker compose restart commander
```

Confirm `worker`'s logs show its watchdog ticks continuing uninterrupted (network and NAS-uptime),
and that a watchdog-channel alert can still be triggered while `commander` is restarting.

### 11.4 General

Verify that setting an edge/NAS/home-network value to something invalid (e.g. a space in
`EDGE_SSH_USER`) makes only that one command fail with a Discord error reply in Commander, without
crashing either container or affecting other commands/watchdog ticks.

The Docker image contains `openssh-client` and `iputils-ping`, available to both containers. If
network status unexpectedly fails after an image rebuild, the focused diagnostic is
`docker compose exec commander ping -c 1 <MIKROTIK_HOST>` (or `exec worker` for the watchdog side)
from the VPS.

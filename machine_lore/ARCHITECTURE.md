# Discord Commander Architecture

> Last reviewed: 2026-08-14
>
> Scope: repository-defined components, communication paths, trust boundaries, deployment, and
> configuration ownership.

Discord Commander is a private operational control plane, split across **two containers built
from the same image and repository**:

- `discord-commander` (`commander/`) -- handles every user-triggered Discord action (slash
  commands, panel buttons, confirmations). Nothing in it runs on a timer.
- `worker-pooling` (`worker/`) -- runs the always-on background jobs (Network Watchdog, NAS Uptime
  Reminder) that have no user waiting on them.

Both connect outbound to Discord, get startup configuration from Infisical, and reach private
home-network systems over the existing routed path. Neither exposes a webhook or an inbound
application port, and neither calls the other directly -- there is no HTTP/queue coupling between
the two containers. Low-level reusable code (Turso client, Discord notifier, network/NAS/MikroTik
probes, config loading) lives in `shared/` and is imported by both.

Runtime details are in [WORKFLOW.md](./WORKFLOW.md). Repository-visible security and reliability
findings are in [AUDIT.md](./AUDIT.md). The split's design rationale and a locking pitfall
discovered along the way are in
[`migrations/worker_split_shared_cache.md`](../migrations/worker_split_shared_cache.md).

## 1. System context

```text
 Authorized Discord operator
             |
             | guild slash command / button interaction
             v
      Discord platform
             ^        ^
             |        | outbound REST only (login(), no gateway session)
             |        |
 VPS host    |        |
 +-----------+--------+----+   +--------------------------------------+
 | Docker: discord-commander |   | Docker: worker-pooling               |
 |                            |   |                                       |
 | Commander app -- HTTPS --> Infisical <-- HTTPS -- Worker app            |
 |          |                 |   |          |                            |
 |          | SSH/ICMP        |   |          | SSH/ICMP                   |
 +----------+-----------------+   +----------+---------------------------+
            |                                |
            +----------------+---------------+
                             |
                             v
 Home network
 +-------------------------+------------------+-----------------+
 | MikroTik                | NAS              | Internal edge   |
 | SSH: ping + WoL          | SSH: shutdown,   | SSH: info       |
 | ICMP target for network | uptime script    | script          |
 +-------------------------+------------------+-----------------+
```

Both containers also independently bootstrap and periodically sync their own local Turso replica
of `master_configurations` from Turso Cloud, and Worker pushes `event_history` rows to Turso Cloud
directly (§6).

Discord is the user transport, not the remote execution environment. Discord interactions select
only fixed actions, and only Commander ever receives them -- Worker never registers commands and
never listens for interactions; it only ever sends outbound alert messages. SSH operations happen
from whichever container owns the action, and all target addresses, users, scripts, and device
identifiers originate in Infisical or Turso, never Discord input.

The boundary between the two containers is who starts the work, not merely whether it polls:

```text
user click / slash command  -> entire resulting flow, including any action-scoped polling
                                (e.g. Wake NAS's poll-until-UP), stays in Commander

timer / interval, no user waiting -> Worker
```

Wake NAS and Shutdown NAS are the clearest instance of this: both involve polling after the
initial action, but that polling is scoped to and finishes with the single user-triggered
operation, so it stays entirely in Commander rather than being handed off to Worker.

## 2. Discord channel model

Two different Discord channels serve different purposes and must have distinct IDs:

```text
Allowed user + allowed guild + Control room
  -> slash commands and panel button interactions (Commander only)
  -> public manual operation results in the same control room

Watchdog channel
  -> Commander: one "Commander online" notification per process start, plus Turso DOWN/
     RECOVERED alerts for Commander's own replica sync (see §6)
  -> Worker: network DOWN/RECOVERED, NAS-uptime reminders, and Turso DOWN/RECOVERED alerts for
     Worker's own replica sync
  -> no commands, no buttons, no manual replies from either container
```

Both containers validate their configured channels on startup through Discord's API. The control
room is also a hard authorization condition, so allowing a user in a guild does not let that user
operate the bot elsewhere in the same guild. Only Commander ever posts to the control room; Worker
only ever posts to the watchdog channel.

Application commands are synchronized separately to every configured guild by Commander. This
provides fast command availability and keeps the command surface limited to `DISCORD_GUILD_IDS`
rather than publishing it globally. Worker registers no commands at all.

## 3. Application components

### 3.1 Commander (`commander/`)

| Component | Responsibility | State |
|---|---|---|
| `commander.bot.CommanderBot` | Discord client lifecycle, command registration/sync, channel validation, its own Turso sync task. | Config and task handle in process memory. |
| `InteractionAuthorizer` | Requires allowed guild, control room, and user ID for every interaction. | Immutable config only. |
| `CommanderPanel` | Persistent panel buttons with stable component IDs. | No application state; restored on startup. |
| `PowerConfirmationView` | 60-second, requester-bound confirmation controls. | Ephemeral in-memory view/message reference. |
| `OperatorOperations` | Shared command/button responses and power workflows; reads Worker's latest network-watchdog transition straight out of Turso `event_history` for the `/status net` display instead of holding a live watchdog reference. | References controllers, limiter, power lock. |
| `EdgeController` | Fixed edge information script over SSH. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `RateLimiter` | Per-user window for confirmed wake/shutdown actions. | Timestamps in memory. |
| `PowerOperationState` | Mutual exclusion for wake/shutdown. Commander-local only -- Worker's NAS Uptime Reminder does not consult it (see §8). | Active-operation name in memory. |

### 3.2 Worker Pooling (`worker/`)

| Component | Responsibility | State |
|---|---|---|
| `worker.main` | Process entrypoint: bootstraps its own Turso replica, builds the HTTP-only Discord client and notifier, starts the two watchdog tasks plus its own Turso sync task as independent `asyncio` tasks, handles SIGTERM/SIGINT for graceful shutdown. | Task handles in process memory. |
| `worker.network_watchdog.NetworkWatchdog` | Tracks UP/DOWN network state and sends transition alerts. | Down flag and down-since time in memory, restored from Turso `event_history` at construction and persisted there on every transition (best-effort; see §8); re-reads config each tick. |
| `worker.nas_reminder.NasUptimeWatchdog` | Tracks NAS-on duration and sends reminders. No longer takes a power-operation lock -- see §8 for why none is needed. | Last-alert timestamp in memory, restored from Turso `event_history` (validated against the NAS's live boot epoch before being trusted; see §8) and persisted there on every reminder (best-effort); re-reads config each tick. |

### 3.3 Shared (`shared/`)

Everything below is plain reusable code with no orchestration of its own -- lifecycle ownership
(when it runs, on whose behalf) stays in `commander/` or `worker/`.

| Component | Responsibility | State |
|---|---|---|
| `shared.config` | Infisical/Turso value loading and validation for every configuration group; `load_config()` used by both processes at startup. | Stateless besides a process-local Infisical secrets cache with a TTL. |
| `shared.logger.setup_logger` | Stdout logger factory; Commander uses the `"commander"` logger, Worker uses `"worker"`. | None. |
| `shared.mikrotik.MikroTikClient` | Fixed RouterOS ping and WoL SSH commands. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `shared.nas.NasController` | NAS reachability, wake delegation, shutdown, and boot-epoch lookup. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `shared.network.NetworkChecker` | Runs Linux `ping` directly from the container; also exports `HOME_NETWORK_EVENT_IDENTIFIER`, the `event_history` key both the watchdog and Commander's manual status display read/write by. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `shared.duration.format_duration` | Formats a second count as `"XhYm"`/`"Ym"`; used by the notifier, `OperatorOperations`, and both watchdogs. | None. |
| `shared.notifier.DiscordWatchdogNotifier` | Delivers automatic embeds to the watchdog channel without mentions, including Turso DOWN/reminder/RECOVERED alerts. | Discord client reference and channel ID. |
| `shared.discord_client.create_http_only_client` | Logs a `discord.Client` in without opening a gateway session -- enough for `fetch_channel()`/`send()`. Used only by Worker, so it doesn't need a second live gateway session under the same bot token just to send occasional alerts. | None. |
| `shared.turso.cache_manager.TursoCacheManager` | Owns one local libSQL replica's lifecycle: fresh startup bootstrap (fail-closed), periodic sync from Turso Cloud, and DOWN/reminder/RECOVERED notification state. Local reads (`read_group`, `read_latest_event`) never touch Turso Cloud. Commander and Worker each construct and own an independent instance -- see §6. | Synced connection, dedicated single-thread executor, health/notification state in memory. |
| `shared.turso.writer.TursoProdWriter` | Owns a second, separate embedded-replica connection used only to push application-generated rows (currently `event_history`) to Turso Cloud. Connects lazily on first write; never blocks or fails process startup. Only Worker constructs one -- Commander has no write path since both watchdogs moved to Worker. | Own synced connection and dedicated single-thread executor, independent of `TursoCacheManager`'s. |

## 4. Data and control flows

### 4.1 Manual interaction flow (Commander)

```text
Discord interaction
  -> InteractionAuthorizer
       -> reject privately if DM, wrong guild/channel, or non-allowed user
       -> accept only in control room
  -> OperatorOperations
       -> defer when an SSH/ICMP operation can take time
       -> worker thread for blocking device work
       -> edit/send public embed or attachment in control room
```

The only destructive paths are wake and shutdown. They add a public prompt, requester-bound
confirmation, per-user power rate limit, and the single process-local power lock before execution.
Both remain entirely in Commander, including their post-action polling -- see §1.

### 4.2 Network watchdog flow (Worker)

```text
container ICMP -> MIKROTIK_HOST
  -> optional anchor ICMP control probe
  -> optional delayed confirmation round
  -> Discord watchdog-channel DOWN alert (sent by Worker's HTTP-only Discord client)
  -> in-memory state flips, then a best-effort event_history row is pushed via TursoProdWriter
  -> repeated recovery probes
  -> Discord watchdog-channel RECOVERED alert with downtime
  -> in-memory state flips, then a best-effort event_history row is pushed via TursoProdWriter
```

Only detected state transitions send notifications. The control room does not receive autonomous
network alerts; Commander's manual `/status net` command reads the latest persisted transition out
of its own Turso replica rather than holding a live reference to Worker's watchdog (§3.1) -- see
also §7.3. The Turso Cloud write always happens after the Discord notification, and a failed write
is retried on later ticks rather than dropped -- see §8 and
`migrations/network_watchdog_state_migration.md`.

### 4.3 NAS uptime watchdog flow (Worker)

```text
MikroTik SSH ping -> NAS online?
  -> NAS SSH uptime script -> Unix boot epoch
  -> restored reminder timestamp validated against this boot epoch, applied or discarded once
  -> threshold/reminder calculation
  -> Discord watchdog-channel NAS still online reminder
  -> a best-effort event_history row is pushed via TursoProdWriter
```

This watchdog runs in Worker, a separate process from Wake/Shutdown NAS (Commander). It no longer
takes an explicit "skip while a wake/shutdown is active" branch -- its ordinary online/uptime
gating already covers that window without needing any cross-process signal, see §8. Unlike the
network watchdog's UP/DOWN restore, the NAS reminder timestamp is only valid for the NAS's
*current* uptime streak, so restoring it can only be resolved once a live boot epoch is available
-- see §8 and `migrations/nas_uptime_watchdog_state_migration.md`.

## 5. Trust boundaries

| Boundary | What crosses it | Repository control | External assumption |
|---|---|---|---|
| Discord user -> Commander | Slash commands and component interactions | Guild/channel/user allowlists; requester-bound power confirmation. | Account security and Discord permissions are correctly administered. |
| Commander -> Discord | Command sync, responses, its own startup/Turso-sync watchdog notifications | No normal-message listener; no allowed mentions in watchdog alerts. | Discord availability and bot-token security. |
| Worker -> Discord | Network/NAS/Turso watchdog alerts only, over an HTTP-only login (no gateway session) | No inbound interaction handling at all; no allowed mentions. | Discord availability and bot-token security. |
| Commander/Worker -> Infisical | Universal Auth login and startup secret fetch | Bootstrap credential files, no application values in code. | Least-privilege project/environment/path policy and credential rotation. |
| VPS -> home network | SSH and ICMP traffic from either container | Fixed actions, validation, timeouts, argv subprocesses. | Routed path/WireGuard/firewall expose only intended systems. |
| Commander/Worker -> SSH identities | Read-only mount and optional configured key path, both containers | No key committed to image. | The mounted key and host account are least privilege. |
| Remote scripts -> devices | Shutdown, uptime, edge info scripts | Paths restricted to safe absolute forms. | Scripts and parent directories are not writable by the automation account. |

## 6. Configuration and secret ownership

```text
VPS files                            Infisical                    Turso Cloud
---------                            ---------                    -----------
~/secrets/infisical/*       ->       Discord token/IDs             master_configurations (read by
                                      MikroTik host/user            each container's own
                                      Turso URL/token/intervals     TursoCacheManager) and
                                                                     event_history (written by
                                                                     Worker's TursoProdWriter)
              |                             |                             |
              +-- Docker secrets --> both containers at startup   bootstrap + periodic pull (read,
                                             |                      both containers, independently) /
                                             v                      lazy connect + push on write
                                   immutable Config snapshot        (write, Worker only)
                                   (Discord + MikroTik only,                |
                                   restart to change)                       v
                                                                /data/turso/local.db per container
                                                                (fresh every start; separate volumes
                                                                 commander_turso_data / worker_turso_data)
```

The Compose environment contains only paths/defaults used to bootstrap Infisical, not application
secrets. `load_config()` materializes Discord identity and MikroTik host/port/username once at
startup into an immutable `Config` snapshot, in **both** processes -- changing those still needs
restarting both containers. Edge, NAS, and home-network operational values (including the shared
SSH key path) are the opposite: they live in Turso (`master_configurations`, source of truth in
Turso Cloud) and are re-read fresh from whichever container's own local replica by
`TursoCacheManager.read_group()` on every command and every watchdog tick, so an edit there applies
on the next action once that container's periodic sync has pulled it in, with no restart. The Turso
connection/sync settings themselves (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
`TURSO_DB_SYNC_INTERVAL`, `TURSO_DB_DOWN_REMINDER`) stay on Infisical, read once at startup by both
processes, since they must exist before either container's Turso replica does.

**Commander and Worker each own an independent local Turso replica** rather than sharing one file.
An earlier version of this split tried a single shared replica (Worker as sole bootstrap/sync
owner, Commander read-only), which looked reasonable but failed in deployment: `pyturso`'s
synchronized connection holds an OS-level lock on the replica's `-wal` file for its entire process
lifetime, so a second process's read connections against the same file fail outright, not
intermittently. See
[`migrations/worker_split_shared_cache.md`](../migrations/worker_split_shared_cache.md) for the
full story. Each replica is deleted and rebuilt fresh from Turso Cloud on its own container's every
start (§7).

`TursoProdWriter` is a separate, one-directional write path layered on top of the same Turso Cloud
database, constructed only by Worker and shared by both watchdogs that need it: `NetworkWatchdog`
pushes an `event_history` row (`identifier="home_network"`, `current_state` = `"UP"`/`"DOWN"`) on
every transition, and `NasUptimeWatchdog` pushes one (`identifier="nas"`,
`current_state="REMINDER_SENT"`) every time it sends a reminder -- both only after the
corresponding Discord notification has already succeeded. One writer instance is reused for both
identifiers/tables rather than one writer per watchdog. It uses its own `":memory:"`-backed
connection and dedicated executor, independent of either process's `TursoCacheManager` --
`TursoCacheManager` never writes, and `TursoProdWriter` never reads application configuration.
`":memory:"` rather than a file is deliberate: `pyturso`'s sync API has no remote-only mode (a
local store is always required, even for a write-only connection that never reads it back), but
this writer never needs that local store to outlive a single write, so it needs no Docker volume,
directory, or path configuration at all. Commander reads the rows Worker writes back out of its
*own* replica once its own periodic sync has pulled them in -- see §8 and
[`network_watchdog_state_migration.md`](../migrations/network_watchdog_state_migration.md) /
[`nas_uptime_watchdog_state_migration.md`](../migrations/nas_uptime_watchdog_state_migration.md) for
the full write/restore design and its failure semantics -- the two watchdogs' restore logic differs
in an important way (the NAS one needs a live boot-epoch check, the network one does not) that is
only documented there, not repeated here.

Only Worker's `TursoCacheManager` has a notifier attached (`attach_notifier()`), so Turso
DOWN/RECOVERED alerts for *that* replica's sync health are sent exactly once even though Commander
syncs its own replica independently and just as continuously; a Turso Cloud outage affects both
replicas' sync at the same time, so one alert is sufficient.

The detailed secret catalog, defaults, and validation rules are in
[WORKFLOW.md](./WORKFLOW.md#9-configuration-reference); the full Turso bootstrap/sync/notification
lifecycle is in [WORKFLOW.md §1](./WORKFLOW.md#1-runtime-overview) and
[§7.3](./WORKFLOW.md#73-turso-synchronization-watchdog).

## 7. Container deployment

One Docker image, built from one `Dockerfile`, based on `python:3.11-slim` and installing:

- `openssh-client` for all SSH paths (used by both containers);
- `iputils-ping` for manual and watchdog network probes (used by both containers);
- Python dependencies from `requirements.txt` (`discord.py==2.5.1`, `infisicalsdk`, and
  `pyturso==0.7.2` for the Turso/libSQL local replica and its synchronization watchdog).

`docker-compose.yml` declares two services from that same image, differing only in command and
volume:

| Service | `container_name` | Command | Turso volume |
|---|---|---|---|
| `commander` | `discord-commander` | `python -m commander.bot` | `commander_turso_data:/data/turso` |
| `worker` | `worker-pooling` | `python -m worker.main` | `worker_turso_data:/data/turso` |

Both have `restart: unless-stopped`, publish no ports, mount the VPS `~/.ssh` directory read-only
at `/root/.ssh`, and mount their *own* dedicated Turso volume at `/data/turso` for their own
`TursoCacheManager`'s `local.db` read replica (intentionally deleted and rebuilt from Turso Cloud
on every container start; see [WORKFLOW.md §7.3](./WORKFLOW.md#73-turso-synchronization-watchdog)).
The two volumes are never shared between containers (§6). `TursoProdWriter`, constructed only in
Worker, needs no volume of its own -- it connects with `":memory:"` as its local store (§6), since
`pyturso` requires one for every connection but this write-only path never reads it back. The broad
SSH mount and current host key policy are deliberate topics in [AUDIT.md](./AUDIT.md), not
endorsements of that long-term deployment shape.

## 8. Availability and state recovery

### 8.1 Failure isolation between containers

This is the primary reliability property the two-container split exists for: neither container's
failure interrupts the other.

```text
worker-pooling crashes/restarts       discord-commander crashes/restarts
  -> Commander: unaffected,             -> Worker: unaffected, network/NAS
     Discord commands, Wake/Shutdown       watchdog ticks and Turso sync
     NAS, manual checks, panel all         keep running and keep sending
     keep working                          watchdog-channel alerts
  -> Worker: background alerts stop      -> Commander: no user interaction
     until it comes back up                 available until it comes back up
```

Neither container calls the other over HTTP or any other channel to check this -- it falls out
directly from them being separate `asyncio` event loops in separate processes with independent
Turso replicas (§6) and independent Discord sessions (§5).

### 8.2 Process-local state lost on restart

The restart policy restarts each container's process after a failure. The following state is
local to the running Commander process and is lost on its restart:

- wake/shutdown lock state (`PowerOperationState`);
- per-user power rate-limit history;
- active 60-second confirmation views.

The persistent panel itself is recoverable after a Commander restart because its component IDs are
registered in `setup_hook()`.

### 8.3 Watchdog state restore (Worker)

The network watchdog's down flag/down-since time and the NAS watchdog's last-reminder timestamp are
both restored from Turso Cloud's `event_history` table rather than being purely memory-only, via
the same `TursoCacheManager.read_latest_event`-to-restore / `TursoProdWriter.record_event`-to-persist
shape, always *after* the corresponding Discord notification has already succeeded. Both are
best-effort mirrors, not a hard guarantee: persisting depends on Turso Cloud being reachable at that
moment, which is a strictly weaker guarantee than the in-memory Discord-notification path each sits
behind. A failed persist is retried on later watchdog ticks (`_pending_persist` on both classes)
rather than dropped, but if the container is rebuilt again before a retry succeeds, restore falls
back to whatever the last successfully persisted row says.

The two restores are not symmetric, though:

- `NetworkWatchdog` restores fully at construction time, purely from the local Turso replica -- an
  UP/DOWN transition is self-contained and needs no other live signal to be trusted on restore.
- `NasUptimeWatchdog` restores the raw timestamp at construction, but only *applies* it once the
  first tick fetches a live boot epoch from the NAS, because a reminder timestamp is only valid for
  the NAS's current uptime streak: if the NAS itself rebooted since that reminder was sent, the
  restored value is stale and must be discarded rather than wrongly suppressing a reminder that is
  actually due again (`restored_alert_at >= boot_epoch`).

See [`network_watchdog_state_migration.md`](../migrations/network_watchdog_state_migration.md) and
[`nas_uptime_watchdog_state_migration.md`](../migrations/nas_uptime_watchdog_state_migration.md) for
the full designs and their explicit scenarios.

### 8.4 No cross-process wake/shutdown coordination, by design

Before the split, a single in-memory `PowerOperationState` was shared between the code that
executes Wake/Shutdown and the NAS uptime watchdog, so the watchdog could skip a tick while a power
operation was in flight. Now that the watchdog runs in a different process (Worker) than
Wake/Shutdown (Commander), that in-memory link can't exist, and **no Turso-backed replacement was
built** -- it isn't needed. `NasUptimeWatchdog._tick()`'s ordinary gating already covers the window
correctly on its own: `is_online()` is `False` for the entire time the NAS is still booting (Wake)
or already down (Shutdown), and once it's back online, uptime starts near 0 seconds, far under the
configured reminder threshold. See
[`migrations/worker_split_shared_cache.md`](../migrations/worker_split_shared_cache.md) for the
full reasoning.

## 9. Ownership map

| Concern | Source of truth |
|---|---|
| User-triggered Discord behaviour | `commander/` in this repository |
| Background watchdog behaviour | `worker/` in this repository |
| Reusable low-level code (config, Turso, probes, notifier) | `shared/` in this repository |
| Operator documentation | `README.md` and `machine_lore/` in this repository |
| Container build/runtime declaration | `Dockerfile` (one image), `docker-compose.yml` (two services) |
| Discord identity, MikroTik identity, Turso connection/sync settings | Infisical |
| Edge, NAS, and home-network operational values (`master_configurations`) | Turso Cloud |
| Network/NAS watchdog operational history (`event_history`) | Turso Cloud (written by Worker's `TursoProdWriter`) |
| Universal Auth bootstrap files | VPS `~/secrets/infisical/` |
| Discord guild/channel roles and bot install | Discord administration |
| VPS firewall, Docker daemon, host SSH, mounted identities | VPS administration |
| Private routing/WireGuard and MikroTik access rules | Network administration |
| NAS/edge SSH accounts, sudoers, and scripts | Device administration |

Keep operational values in their owning system. Do not copy bot tokens, SSH private keys, Discord
IDs, or current host details into this documentation.

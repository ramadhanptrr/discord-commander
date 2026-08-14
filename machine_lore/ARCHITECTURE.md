# Discord Commander Architecture

> Last reviewed: 2026-08-08
>
> Scope: repository-defined components, communication paths, trust boundaries, deployment, and
> configuration ownership.

Discord Commander is a private operational control plane. A Docker container on a VPS connects
outbound to Discord, gets startup configuration from Infisical, and reaches private home-network
systems over the existing routed path. It does not expose a webhook or an inbound application port.

Runtime details are in [WORKFLOW.md](./WORKFLOW.md). Repository-visible security and reliability
findings are in [AUDIT.md](./AUDIT.md).

## 1. System context

```text
 Authorized Discord operator
             |
             | guild slash command / button interaction
             v
      Discord platform
             ^
             | outbound Discord gateway + REST connection
             |
 VPS host
 +----------------------------------------------------------------+
 | Docker: discord-commander                                     |
 |                                                                |
 |  Commander application -------- HTTPS --------> Infisical      |
 |          |                                                     |
 |          | SSH or ICMP over existing private/routed path       |
 +----------+-----------------------------------------------------+
            |
            v
 Home network
 +-------------------------+------------------+-----------------+
 | MikroTik                | NAS              | Internal edge   |
 | SSH: ping + WoL          | SSH: shutdown,   | SSH: info       |
 | ICMP target for network | uptime script    | script          |
 +-------------------------+------------------+-----------------+
```

Discord is the user transport, not the remote execution environment. Discord interactions select
only fixed actions. SSH operations happen from the container, and all target addresses, users,
scripts, and device identifiers originate in Infisical.

## 2. Discord channel model

Two different Discord channels serve different purposes and must have distinct IDs:

```text
Allowed user + allowed guild + Control room
  -> slash commands and panel button interactions
  -> public manual operation results in the same control room

Watchdog channel
  -> automatic Commander-started, network DOWN/RECOVERED, and NAS-uptime notifications only
  -> no commands, no buttons, no manual replies
```

The application validates both channels on startup through Discord's API. The control room is also a
hard authorization condition, so allowing a user in a guild does not let that user operate the bot
elsewhere in the same guild.

Application commands are synchronized separately to every configured guild. This provides fast
command availability and keeps the command surface limited to `DISCORD_GUILD_IDS` rather than
publishing it globally.

## 3. Application components

| Component | Responsibility | State |
|---|---|---|
| `commander.bot.CommanderBot` | Discord client lifecycle, command registration/sync, channel validation, watchdog task lifecycle. | Config and task handles in process memory. |
| `InteractionAuthorizer` | Requires allowed guild, control room, and user ID for every interaction. | Immutable config only. |
| `CommanderPanel` | Persistent panel buttons with stable component IDs. | No application state; restored on startup. |
| `PowerConfirmationView` | 60-second, requester-bound confirmation controls. | Ephemeral in-memory view/message reference. |
| `OperatorOperations` | Shared command/button responses and power workflows. | References controllers, limiter, power lock, network watchdog. |
| `MikroTikClient` | Fixed RouterOS ping and WoL SSH commands. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `NasController` | NAS reachability, wake delegation, shutdown, and boot-epoch lookup. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `EdgeController` | Fixed edge information script over SSH. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `NetworkChecker` | Runs Linux `ping` directly from the container. | Holds `TursoCacheManager`; re-reads config fresh on every call. |
| `NetworkWatchdog` | Tracks UP/DOWN network state and sends transition alerts. | Down flag and down-since time in memory; re-reads config each tick. |
| `NasUptimeWatchdog` | Tracks NAS-on duration and sends reminders. | Last-alert timestamp in memory; re-reads config each tick. |
| `DiscordWatchdogNotifier` | Delivers automatic embeds to watchdog channel without mentions, including Turso DOWN/reminder/RECOVERED alerts. | Bot reference and channel ID. |
| `RateLimiter` | Per-user window for confirmed wake/shutdown actions. | Timestamps in memory. |
| `PowerOperationState` | Mutual exclusion for wake/shutdown. | Active-operation name in memory. |
| `TursoCacheManager` | Owns the local libSQL replica lifecycle: fresh startup bootstrap (fail-closed), periodic sync from Turso Cloud, and DOWN/reminder/RECOVERED notification state. Local reads (`read_group`) never touch Turso Cloud. | Synced connection, dedicated single-thread executor, health/notification state in memory. |

## 4. Data and control flows

### 4.1 Manual interaction flow

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

### 4.2 Network watchdog flow

```text
container ICMP -> MIKROTIK_HOST
  -> optional anchor ICMP control probe
  -> optional delayed confirmation round
  -> Discord watchdog-channel DOWN alert
  -> repeated recovery probes
  -> Discord watchdog-channel RECOVERED alert with downtime
```

Only detected state transitions send notifications. The control room does not receive autonomous
network alerts; manual status commands remain separate from alert state.

### 4.3 NAS uptime watchdog flow

```text
MikroTik SSH ping -> NAS online?
  -> NAS SSH uptime script -> Unix boot epoch
  -> threshold/reminder calculation
  -> Discord watchdog-channel NAS still online reminder
```

During a wake or shutdown operation the uptime watchdog deliberately skips work, preventing normal
power transitions from being framed as forgotten-online incidents.

## 5. Trust boundaries

| Boundary | What crosses it | Repository control | External assumption |
|---|---|---|---|
| Discord user -> bot | Slash commands and component interactions | Guild/channel/user allowlists; requester-bound power confirmation. | Account security and Discord permissions are correctly administered. |
| Bot -> Discord | Command sync, responses, watchdog notifications | No normal-message listener; no allowed mentions in watchdog alerts. | Discord availability and bot-token security. |
| Bot -> Infisical | Universal Auth login and startup secret fetch | Bootstrap credential files, no application values in code. | Least-privilege project/environment/path policy and credential rotation. |
| VPS -> home network | SSH and ICMP traffic | Fixed actions, validation, timeouts, argv subprocesses. | Routed path/WireGuard/firewall expose only intended systems. |
| Bot -> SSH identities | Read-only mount and optional configured key path | No key committed to image. | The mounted key and host account are least privilege. |
| Remote scripts -> devices | Shutdown, uptime, edge info scripts | Paths restricted to safe absolute forms. | Scripts and parent directories are not writable by the automation account. |

## 6. Configuration and secret ownership

```text
VPS files                            Infisical                    Turso Cloud
---------                            ---------                    -----------
~/secrets/infisical/*       ->       Discord token/IDs             (nothing reads it directly;
                                      MikroTik host/user            TursoCacheManager owns the
                                      Turso URL/token/intervals     only connection)
              |                             |                             |
              +-- Docker secrets --> Commander at startup           bootstrap + periodic pull
                                             |                             |
                                             v                             v
                                   immutable Config snapshot   /data/turso/local.db (edge, nas,
                                   (Discord + MikroTik only,   home_network groups; read fresh
                                   restart to change)          on every action, no restart)
```

The Compose environment contains only paths/defaults used to bootstrap Infisical, not application
secrets. `load_config()` materializes Discord identity and MikroTik host/port/username once at
startup into an immutable `Config` snapshot -- changing those still needs a restart. Edge, NAS, and
home-network operational values (including the shared SSH key path) are the opposite: they live in
Turso (`master_configurations`, source of truth in Turso Cloud) and are re-read fresh from the local
replica by `TursoCacheManager.read_group()` on every command and every watchdog tick, so an edit
there applies on the next action once periodic sync has pulled it in, with no restart. The Turso
connection/sync settings themselves (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
`TURSO_DB_SYNC_INTERVAL`, `TURSO_DB_DOWN_REMINDER`) stay on Infisical, read once at startup, since
they must exist before the Turso replica does.

The detailed secret catalog, defaults, and validation rules are in
[WORKFLOW.md](./WORKFLOW.md#9-configuration-reference); the full Turso bootstrap/sync/notification
lifecycle is in [WORKFLOW.md §1](./WORKFLOW.md#1-runtime-overview) and
[§7.3](./WORKFLOW.md#73-turso-synchronization-watchdog).

## 7. Container deployment

The Docker image is based on `python:3.11-slim` and installs:

- `openssh-client` for all SSH paths;
- `iputils-ping` for manual and watchdog network probes;
- Python dependencies from `requirements.txt` (`discord.py==2.5.1`, `infisicalsdk`, and
  `pyturso==0.7.2` for the Turso/libSQL local replica and its synchronization watchdog).

The container runs `python -m commander.bot`, has `restart: unless-stopped`, publishes no ports,
mounts the VPS `~/.ssh` directory read-only at `/root/.ssh`, and mounts a dedicated
`commander_turso_data` volume at `/data/turso` for the local Turso replica (intentionally deleted
and rebuilt from Turso Cloud on every container start; see
[WORKFLOW.md §7.3](./WORKFLOW.md#73-turso-synchronization-watchdog)). The broad SSH mount and
current host key policy are deliberate topics in [AUDIT.md](./AUDIT.md), not endorsements of that
long-term deployment shape.

## 8. Availability and state recovery

The restart policy restarts the process after a failure. The following state is local to the running
process and is lost on restart:

- wake/shutdown lock state;
- per-user power rate-limit history;
- active 60-second confirmation views;
- network watchdog down flag and downtime start time;
- NAS watchdog last-reminder timestamp.

The persistent panel itself is recoverable after restart because its component IDs are registered in
`setup_hook()`. The NAS watchdog can reconstruct actual NAS uptime from the boot epoch returned by
the NAS, but it cannot preserve the exact last reminder time; an overdue NAS may therefore alert
again after a restart. The network watchdog has no external outage timestamp and reports a new
outage from the restarted process if the network is still unreachable.

## 9. Ownership map

| Concern | Source of truth |
|---|---|
| Discord Commander application behaviour | `commander/` in this repository |
| Operator documentation | `README.md` and `machine_lore/` in this repository |
| Container build/runtime declaration | `Dockerfile`, `docker-compose.yml` |
| Discord identity, MikroTik identity, Turso connection/sync settings | Infisical |
| Edge, NAS, and home-network operational values (`master_configurations`) | Turso Cloud |
| Universal Auth bootstrap files | VPS `~/secrets/infisical/` |
| Discord guild/channel roles and bot install | Discord administration |
| VPS firewall, Docker daemon, host SSH, mounted identities | VPS administration |
| Private routing/WireGuard and MikroTik access rules | Network administration |
| NAS/edge SSH accounts, sudoers, and scripts | Device administration |

Keep operational values in their owning system. Do not copy bot tokens, SSH private keys, Discord
IDs, or current host details into this documentation.


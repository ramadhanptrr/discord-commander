# Discord Commander

> A small Discord control plane for a NAS that should be off more often than it is on.

Discord Commander is the Discord transport for the same practical idea behind Telegram Commander:
keep an infrequently used NAS powered down, wake it only when needed, and retain a safe way to
check or shut it down from outside the home network.

It runs as a Docker container on a VPS. The container reaches the home network through the
existing routed path, uses SSH for MikroTik/NAS/edge operations, and reads its configuration from
Infisical at startup.

## The origin story

The NAS does not need to spin disks all day just to be useful for the occasional backup. Keeping it
off by default reduces unnecessary power use and avoids making an already fragile power environment
part of the disk-lifetime plan. Wake-on-LAN, a MikroTik router, and a private Discord control room
make the machine available when it is actually needed.

Discord Commander is deliberately not a general-purpose remote shell. Every action is fixed in
code or in a validated Infisical value. The point is a narrow control plane, not a convenient way to
run arbitrary commands from chat.

## Read the machine lore

| Document | What it covers |
|---|---|
| [WORKFLOW.md](./machine_lore/WORKFLOW.md) | Startup, Discord interactions, buttons, device actions, watchdogs, configuration, and VPS smoke tests |
| [ARCHITECTURE.md](./machine_lore/ARCHITECTURE.md) | Components, network paths, trust boundaries, state, deployment, and ownership |
| [AUDIT.md](./machine_lore/AUDIT.md) | Repository-visible security/reliability controls, remaining risks, and production verification |

## Features

### Manual control room operations

All manual actions must be issued by an allowed user in `DISCORD_CONTROL_ROOM_CHANNEL_ID`. Results
are posted publicly in that control room so the operator history remains visible to the team.

| Slash command | Panel button | Result |
|---|---|---|
| `/ping` | Ping | Confirms the bot is responding. |
| `/status nas` | Status NAS | Asks MikroTik to check NAS reachability. |
| `/status net` | Status Network | Runs an ICMP probe from the container to the home gateway. |
| `/edge info` | Edge Info | Runs the fixed `EDGE_INFO_SCRIPT` through SSH and returns its output. |
| `/wake nas` | Wake NAS | Shows a 60-second confirmation, reports when the NAS is already online, or sends Wake-on-LAN through MikroTik and waits up to two minutes for it. |
| `/shutdown nas` | Shutdown NAS | Shows a 60-second confirmation, runs the fixed graceful shutdown script through SSH, then confirms when the NAS stops responding. |
| `/panel` | — | Posts the persistent interactive control panel. |
| `/start` | — | Posts the welcome message and the same control panel. |
| `/help` | Help | Lists all supported commands. |

The panel has persistent component IDs, so a panel message remains usable after a container restart
as long as the bot comes back with the same code. Power-confirmation buttons are intentionally
short-lived and expire after 60 seconds.

### Automatic watchdog alerts

The watchdog channel is notification-only. It must be different from the control room and it never
receives panel buttons or ordinary command replies.

| Watchdog | Destination | Behaviour |
|---|---|---|
| Home network | `DISCORD_WATCHDOG_CHANNEL_ID` | Sends one **DOWN** alert after configured failed/confirmed probes, then one **RECOVERED** alert when the gateway answers again. It stays quiet while the state is unchanged. |
| NAS uptime | `DISCORD_WATCHDOG_CHANNEL_ID` | Reminds operators when the NAS remains online longer than `NAS_MAX_AGE_MINUTE`, then repeats at `NAS_MAX_AGE_REMINDER_MINUTE` intervals while it remains online. |

Manual `/status net` does not generate a watchdog alert or alter watchdog state. It replies in the
control room and, when an outage is already tracked, includes the current watchdog downtime.

## Discord prerequisites

Install the bot in every guild listed by `DISCORD_GUILD_IDS` with the `bot` and
`applications.commands` scopes. The bot needs access to both configured channels:

- **Control room:** View Channel, Send Messages, Embed Links, Attach Files, and permission to use
  application-command interactions.
- **Watchdog channel:** View Channel, Send Messages, and Embed Links.

The application intentionally does not read normal Discord messages. It uses guild-scoped slash
commands and button interactions only; the Message Content privileged intent is not required.

## Quick deployment on the VPS

This repository is built on the VPS. There is no requirement to run the bot locally.

```bash
git pull
docker compose build
docker compose up -d
docker compose logs --tail=100 -f
```

After a code or dependency change, rebuild and recreate with `docker compose build` followed by
`docker compose up -d`. After changing only an Infisical application value, restart the service so
it materializes fresh startup configuration:

```bash
docker compose restart commander
```

Use the smoke-test checklist in [WORKFLOW.md](./machine_lore/WORKFLOW.md#11-vps-deployment-and-smoke-test)
after each deployment. Do not test `/wake nas` or `/shutdown nas` outside an appropriate maintenance
window: they intentionally change power state.

## Configuration at a glance

Bootstrap credentials are mounted as Docker secrets; application values are fetched from Infisical.
No token or device credential belongs in this repository or a Compose environment file.

| Group | Required Infisical values |
|---|---|
| Discord access | `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_IDS`, `DISCORD_CONTROL_ROOM_CHANNEL_ID`, `DISCORD_WATCHDOG_CHANNEL_ID`, `DISCORD_ALLOWED_USER_IDS` |
| MikroTik | `MIKROTIK_HOST`, `MIKROTIK_USERNAME`, `NAS_IP`, `NAS_MAC`, `NAS_WOL_INTERFACE` |
| NAS actions/watchdog | `NAS_USER`, `NAS_SHUTDOWN_SCRIPT`, `NAS_UPTIME_SCRIPT`, `NAS_MAX_AGE_MINUTE`, `NAS_MAX_AGE_REMINDER_MINUTE` |
| Edge info | `EDGE_INTERNAL_IP`, `EDGE_SSH_USER`, `EDGE_INFO_SCRIPT` |

Ports, timing values, optional `SSH_KEY_PATH`, and all defaults are documented in the
[configuration reference](./machine_lore/WORKFLOW.md#9-configuration-reference).

## Stack

- Python 3.11 and `discord.py` 2.5.1
- Discord guild application commands and persistent UI components
- Infisical Universal Auth for startup configuration
- OpenSSH client for MikroTik, NAS, and edge-host operations
- `iputils-ping` for direct home-network probes
- Docker Compose on the VPS
- Existing private/routed path to the home network

*The router is still doing router things. The disks get to rest.*

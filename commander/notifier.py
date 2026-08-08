from __future__ import annotations

import discord

from commander.netmonitor import format_duration


class DiscordWatchdogNotifier:
    """Sends autonomous Commander notifications to the dedicated watchdog channel only."""

    def __init__(self, bot: discord.Client, watchdog_channel_id: int, interval_seconds: int) -> None:
        self._bot = bot
        self._watchdog_channel_id = watchdog_channel_id
        self._interval_seconds = interval_seconds

    async def _channel(self) -> discord.abc.Messageable:
        channel = self._bot.get_channel(self._watchdog_channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(self._watchdog_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("Configured watchdog channel does not accept messages")
        return channel

    async def send_commander_started(self) -> None:
        embed = discord.Embed(
            title="🟢 Commander online",
            description="Discord Commander started and is ready for control-room operations.",
            colour=discord.Colour.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Commander lifecycle • Automatic notification")
        await (await self._channel()).send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_network_down(self, host: str, checks: list[str]) -> None:
        embed = discord.Embed(
            title="🔴 Home network DOWN",
            description=f"`{host}` is unreachable.",
            colour=discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Verification", value=", ".join(checks), inline=False)
        embed.set_footer(text="Network watchdog • Automatic alert")
        await (await self._channel()).send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_network_recovered(self, host: str, downtime: float | None) -> None:
        duration = format_duration(downtime) if downtime is not None else "unknown"
        interval = format_duration(self._interval_seconds)
        embed = discord.Embed(
            title="🟢 Home network RECOVERED",
            description=f"`{host}` is reachable again.",
            colour=discord.Colour.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Downtime", value=f"~{duration}", inline=True)
        embed.add_field(name="Check interval", value=f"~{interval}", inline=True)
        embed.set_footer(text="Network watchdog • Automatic alert")
        await (await self._channel()).send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_nas_uptime_reminder(self, uptime: float) -> None:
        duration = format_duration(uptime)
        embed = discord.Embed(
            title="⚠️ NAS still online",
            description=f"NAS has been online for {duration}.",
            colour=discord.Colour.yellow(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Suggested action",
            value="Use `/shutdown nas` or the Commander control panel when it is no longer needed.",
            inline=False,
        )
        embed.set_footer(text="NAS uptime watchdog • Automatic reminder")
        await (await self._channel()).send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

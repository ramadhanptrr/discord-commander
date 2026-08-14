from __future__ import annotations

import discord


async def create_http_only_client(bot_token: str) -> discord.Client:
    """Log in without opening a gateway session.

    Enough for ``DiscordWatchdogNotifier`` (fetch_channel()/send() are plain REST calls); avoids
    opening a second live gateway session under the same bot token just to send occasional
    background alerts. See migrations/worker_split_shared_cache.md.
    """
    client = discord.Client(intents=discord.Intents.none())
    await client.login(bot_token)
    return client

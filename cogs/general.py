"""General-purpose / utility commands.

A small starter set of commands. This is the easiest place to add new
"other stuff" as the bot grows.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check that the bot is alive and see its latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: {latency_ms}ms", ephemeral=True)

    @app_commands.command(name="floofcount", description="See how many members currently have the Floofs role.")
    async def floofcount(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        role_id = getattr(self.bot.config, "floofs_role_id", None)
        if guild is None or not role_id:
            await interaction.response.send_message(
                "The Floofs role isn't configured yet.", ephemeral=True
            )
            return
        role = guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                f"Couldn't find a role with ID `{role_id}` in this server. "
                "Double-check the `FLOOFS_ROLE_ID` variable — run `/roles` to see the real IDs.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"🐾 There are **{len(role.members)}** floofs in the server!", ephemeral=True
        )


    @app_commands.command(name="roles", description="List every role and its ID (handy for configuring the bot).")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def roles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        # Highest roles first; skip @everyone. Show name and copy-pasteable ID.
        lines = [
            f"`{role.id}` — {role.name}"
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not role.is_default()
        ]
        if not lines:
            await interaction.response.send_message("This server has no roles.", ephemeral=True)
            return
        # Stay under Discord's 2000-char message limit.
        text = "**Roles in this server (newest at top):**\n" + "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n… (list truncated)"
        await interaction.response.send_message(text, ephemeral=True)

    @roles.error
    async def roles_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = (
            "You need the **Manage Roles** permission to use this."
            if isinstance(error, app_commands.MissingPermissions)
            else "Something went wrong listing roles."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))

"""Staff commands to view and change runtime settings, persisted to the
shared store (Nextcloud). Lets staff tune the bot from Discord instead of
editing Railway environment variables.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from checks import NotStaff, is_staff
from settings import SETTINGS

log = logging.getLogger("furbot.config")


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="config", description="View or change bot settings (staff only).")

    async def _key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        return [
            app_commands.Choice(name=key, value=key)
            for key in SETTINGS
            if cur in key
        ][:25]

    @group.command(name="view", description="Show all settings and their current values.")
    @is_staff()
    async def view(self, interaction: discord.Interaction) -> None:
        settings = self.bot.settings
        embed = discord.Embed(title="⚙️ FurBot settings", color=discord.Color.blurple())
        for key, (typ, default, help_text) in SETTINGS.items():
            value = settings.get(key)
            source = settings.source(key)
            shown = repr(value) if value != "" else "(empty)"
            embed.add_field(
                name=key,
                value=f"**{shown}**  ·  _{source}_\n{help_text}\n`default: {default!r}`",
                inline=False,
            )
        embed.set_footer(text="Change with /config set <key> <value> — saved to Nextcloud.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="set", description="Change a setting (saved to Nextcloud, applies live).")
    @app_commands.describe(key="Which setting to change", value="The new value")
    @app_commands.autocomplete(key=_key_autocomplete)
    @is_staff()
    async def set_cmd(self, interaction: discord.Interaction, key: str, value: str) -> None:
        if key not in SETTINGS:
            await interaction.response.send_message(
                f"Unknown setting `{key}`. Use `/config view` to see the list.", ephemeral=True
            )
            return
        old = self.bot.settings.get(key)
        try:
            new = await self.bot.settings.set(key, value)
        except ValueError:
            typ = SETTINGS[key][0].__name__
            await interaction.response.send_message(
                f"`{value}` isn't a valid **{typ}** for `{key}`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ `{key}`: **{old!r}** → **{new!r}** (saved).", ephemeral=True
        )

    @group.command(name="reset", description="Reset a setting back to its default.")
    @app_commands.describe(key="Which setting to reset")
    @app_commands.autocomplete(key=_key_autocomplete)
    @is_staff()
    async def reset_cmd(self, interaction: discord.Interaction, key: str) -> None:
        if key not in SETTINGS:
            await interaction.response.send_message(f"Unknown setting `{key}`.", ephemeral=True)
            return
        removed = await self.bot.settings.reset(key)
        now = self.bot.settings.get(key)
        msg = f"↩️ `{key}` reset — now **{now!r}**." if removed else f"`{key}` had no override; it's **{now!r}**."
        await interaction.response.send_message(msg, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Command error in Config cog", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigCog(bot))

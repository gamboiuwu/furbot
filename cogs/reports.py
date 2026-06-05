"""Member safety reporting.

A single, always-accessible button (pinned in a public channel) lets any member
file a one-message report. On submit, the bot posts the report to a private
staff-only channel and pings the safety team, so nothing gets lost in scattered
DMs or buried messages.

Flow:
  * Staff run `/report setup` once to post (and pin) the report button in the
    configured hub channel.
  * A member taps the button, types what's going on (plus, optionally, who/what
    it concerns), and submits.
  * The bot posts an embed to the private safety channel, pinging the safety
    role, and confirms privately to the reporter.

Config (via /config set):
  reports_enabled, reports_channel_id, reports_role_id, reports_hub_channel_id
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.reports")

HUBMSG = "reports_hub_msg"  # {"channel": id, "message": id}


# ============================ report modal ====================================

class ReportModal(discord.ui.Modal, title="Report to the Safety Team"):
    """One-message safety report. Kept short on purpose — staff follow up after."""

    def __init__(self, cog: "Reports") -> None:
        super().__init__()
        self.cog = cog
        self.what = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=True, max_length=1500,
            placeholder="Describe what happened. Include where it happened (channel/DM) and when, if you can.",
        )
        self.who = discord.ui.TextInput(
            required=False, max_length=200,
            placeholder="e.g. a username, or leave blank",
        )
        self.add_item(discord.ui.Label(
            text="What's going on?",
            description="Tell the safety team what happened. This goes only to staff.",
            component=self.what,
        ))
        self.add_item(discord.ui.Label(
            text="Who or what is this about? (optional)",
            description="A username, channel, or anything that helps staff find it.",
            component=self.who,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_report(interaction, self.what.value.strip(), self.who.value.strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("ReportModal failed", exc_info=error)
        msg = ("Something went wrong sending your report. Please reach out to a staff member "
               "directly so this doesn't get missed.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ============================ persistent button ===============================

class ReportHubView(discord.ui.View):
    """Persistent 'Report' button under the pinned hub message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Report to the Safety Team", style=discord.ButtonStyle.danger,
        emoji="🚨", custom_id="reports:open",
    )
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Reports | None" = interaction.client.get_cog("Reports")
        if cog is None:
            await interaction.response.send_message(
                "Reporting isn't available right now — please contact a staff member directly.",
                ephemeral=True,
            )
            return
        await cog.open_report(interaction)


# ================================= cog ========================================

class Reports(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    def _s(self, key: str):
        return self.settings.get(key)

    # ---- member-facing ---------------------------------------------------

    async def open_report(self, interaction: discord.Interaction) -> None:
        if not self._s("reports_enabled"):
            await interaction.response.send_message(
                "Reporting isn't switched on yet — please contact a staff member directly.",
                ephemeral=True,
            )
            return
        if not int(self._s("reports_channel_id") or 0):
            await interaction.response.send_message(
                "Reporting isn't fully set up yet — please contact a staff member directly.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ReportModal(self))

    async def submit_report(self, interaction: discord.Interaction, what: str, who: str) -> None:
        # Acknowledge first — posting to the staff channel is network I/O.
        await interaction.response.defer(ephemeral=True)
        channel_id = int(self._s("reports_channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.error("Safety report could not be delivered — channel %s not found.", channel_id)
            await interaction.followup.send(
                "I couldn't deliver your report (misconfigured channel). Please contact a staff "
                "member directly so this isn't missed.",
                ephemeral=True,
            )
            return

        user = interaction.user
        where = interaction.channel
        embed = discord.Embed(
            title="🚨 New Safety Report",
            description=what[:4000] or "(no description)",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reporter", value=f"{user.mention} (`{user}` · {user.id})", inline=False)
        if who:
            embed.add_field(name="About", value=who[:1024], inline=False)
        if isinstance(where, (discord.TextChannel, discord.Thread)):
            embed.add_field(name="Reported from", value=where.mention, inline=False)
        embed.set_footer(text="Reply in this channel / open a thread to coordinate follow-up.")

        role_id = int(self._s("reports_role_id") or 0)
        content = f"<@&{role_id}>" if role_id else None
        allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException:
            log.exception("Failed to post safety report to channel %s", channel_id)
            await interaction.followup.send(
                "I couldn't deliver your report just now. Please contact a staff member directly "
                "so this isn't missed.",
                ephemeral=True,
            )
            return

        # Keep a lightweight count for visibility (no report content is stored).
        try:
            await self.store.set("reports_count", int(self.store.get("reports_count", 0)) + 1)
        except Exception:
            log.exception("Could not update reports_count")

        await interaction.followup.send(
            "✅ Thank you — your report has been sent privately to the safety team and they've been "
            "pinged. They'll take it from here. If you're in immediate danger, contact local "
            "emergency services.",
            ephemeral=True,
        )

    # ---- staff setup -----------------------------------------------------

    group = app_commands.Group(name="report", description="Safety reporting tools.")

    @group.command(name="setup", description="(Staff) Post or refresh the pinned report button.")
    @is_staff()
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cid = int(self._s("reports_hub_channel_id") or 0)
        channel = interaction.guild.get_channel(cid) if interaction.guild and cid else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Set a valid channel first: `/config set reports_hub_channel_id <channel id>`.",
                ephemeral=True,
            )
            return
        embed = self._hub_embed()
        prev = self.store.get(HUBMSG, {})
        msg = None
        if prev.get("channel") == channel.id and prev.get("message"):
            try:
                msg = await channel.fetch_message(prev["message"])
                await msg.edit(embed=embed, view=ReportHubView())
            except discord.HTTPException:
                msg = None
        if msg is None:
            try:
                msg = await channel.send(embed=embed, view=ReportHubView())
            except discord.Forbidden:
                await interaction.followup.send("I can't post in that channel — check my permissions.", ephemeral=True)
                return
        try:
            await msg.pin(reason="Safety report button")
        except discord.HTTPException:
            log.info("Could not pin the report message (missing permission?)")
        await self.store.set(HUBMSG, {"channel": channel.id, "message": msg.id})

        warn = ""
        if not self._s("reports_enabled"):
            warn += "\n⚠️ `reports_enabled` is off — turn it on with `/config set reports_enabled true`."
        if not int(self._s("reports_channel_id") or 0):
            warn += "\n⚠️ Set the private channel: `/config set reports_channel_id <id>`."
        if not int(self._s("reports_role_id") or 0):
            warn += "\nℹ️ No safety role set — reports will post without a ping. `/config set reports_role_id <id>`."
        await interaction.followup.send(f"✅ Report button posted and pinned in {channel.mention}.{warn}", ephemeral=True)

    @group.command(name="status", description="(Staff) Show reporting configuration.")
    @is_staff()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        ch = int(self._s("reports_channel_id") or 0)
        role = int(self._s("reports_role_id") or 0)
        hub = int(self._s("reports_hub_channel_id") or 0)
        lines = [
            f"**Enabled:** {bool(self._s('reports_enabled'))}",
            f"**Private channel:** {f'<#{ch}>' if ch else '_not set_'}",
            f"**Safety role ping:** {f'<@&{role}>' if role else '_none_'}",
            f"**Button channel:** {f'<#{hub}>' if hub else '_not set_'}",
            f"**Reports received:** {int(self.store.get('reports_count', 0))}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    def _hub_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🚨 Report to the Safety Team",
            description=(
                "See something that doesn't sit right? Tap the button below to send a private "
                "report straight to the safety team — no need to track anyone down.\n\n"
                "• Your report goes to a **staff-only channel** and pings the safety team right away.\n"
                "• Share what happened, and where/when if you can.\n"
                "• If you're in immediate danger, contact local emergency services first.\n\n"
                "We take every report seriously. owo"
            ),
            color=discord.Color.red(),
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Reports command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))

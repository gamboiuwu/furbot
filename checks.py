"""Reusable app-command checks.

`is_staff()` matches the same definition the verification cog uses: a member
counts as staff if they have the configured STAFF_ROLE_ID, or (failing that)
the Manage Roles permission.
"""

from __future__ import annotations

import discord
from discord import app_commands


class NotStaff(app_commands.CheckFailure):
    """Raised when a non-staff member tries to use a staff-only command."""


def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise NotStaff()
        cfg = getattr(interaction.client, "config", None)
        staff_role_id = getattr(cfg, "staff_role_id", None) if cfg else None
        if staff_role_id and any(r.id == staff_role_id for r in member.roles):
            return True
        if member.guild_permissions.manage_roles:
            return True
        raise NotStaff()

    return app_commands.check(predicate)

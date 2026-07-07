import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import utcnow

from bd_models.models import BallInstance, Player

from ballsdex.core.utils.transformers import BallInstanceTransform

from .battle import BattleInstance

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

type Interaction = discord.Interaction["BallsDexBot"]

log = logging.getLogger("ballsdex.packages.battle")

CHALLENGE_EXPIRY = 60 * 5


@dataclass
class PendingChallenge:
    """
    A challenge that has been issued but not yet accepted or declined.
    """

    channel_id: int
    challenger: Player
    challenger_user: discord.abc.User
    challenger_card_id: int
    opponent_user: discord.abc.User
    created_at: datetime = field(default_factory=utcnow)
    expiry_task: "asyncio.Task | None" = None


@app_commands.guild_only()
class Battle(commands.GroupCog):
    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        # channel_id -> {user_id: BattleInstance}, mirroring Trade's self.battles structure. Both
        # participants' user ids point at the same instance so either side can be looked up.
        self.battles: dict[int, dict[int, BattleInstance]] = defaultdict(dict)
        # (channel_id, opponent_user_id) -> PendingChallenge, cleared on accept/decline/expiry.
        self.pending_challenges: dict[tuple[int, int], PendingChallenge] = {}

    async def get_battle(
        self, interaction: Interaction, user: discord.User | discord.Member | None = None
    ) -> None | BattleInstance:
        assert interaction.channel
        user = user or interaction.user
        battle = self.battles.get(interaction.channel.id, {}).get(user.id)
        if not battle:
            return None
        if not battle.active:
            del self.battles[interaction.channel.id][battle.combatant1.user.id]
            del self.battles[interaction.channel.id][battle.combatant2.user.id]
            return None
        return battle

    def _clear_pending(self, key: tuple[int, int]):
        pending = self.pending_challenges.pop(key, None)
        if pending and pending.expiry_task and not pending.expiry_task.done():
            pending.expiry_task.cancel()

    async def _expire_challenge(self, key: tuple[int, int], channel: discord.abc.Messageable):
        await asyncio.sleep(CHALLENGE_EXPIRY)
        pending = self.pending_challenges.pop(key, None)
        if pending is None:
            return
        try:
            await channel.send(
                f"{pending.opponent_user.mention}, the battle challenge from "
                f"{pending.challenger_user.mention} has expired."
            )
        except discord.HTTPException:
            pass

    @app_commands.command()
    @app_commands.checks.bot_has_permissions(send_messages=True)
    async def challenge(self, interaction: Interaction, user: discord.User, card: BallInstanceTransform):
        """
        Challenge someone to a battle.

        Parameters
        ----------
        user: discord.User
            The user you want to challenge.
        card: BallInstanceTransform
            The card you want to fight with.
        """
        assert interaction.channel

        if user.bot:
            await interaction.response.send_message("You cannot battle bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("You cannot battle yourself.", ephemeral=True)
            return
        if user.id in self.bot.blacklist:
            await interaction.response.send_message("You cannot battle a blacklisted user.", ephemeral=True)
            return
        if await self.get_battle(interaction) is not None:
            await interaction.response.send_message("You already have an active battle.", ephemeral=True)
            return
        if await self.get_battle(interaction, user) is not None:
            await interaction.response.send_message("That user already has an active battle.", ephemeral=True)
            return
        if card.player.discord_id != interaction.user.id:
            await interaction.response.send_message("You do not own that card.", ephemeral=True)
            return

        key = (interaction.channel.id, user.id)
        if key in self.pending_challenges:
            await interaction.response.send_message(
                "That user already has a pending challenge waiting in this channel.", ephemeral=True
            )
            return

        player1, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)

        pending = PendingChallenge(
            channel_id=interaction.channel.id,
            challenger=player1,
            challenger_user=interaction.user,
            challenger_card_id=card.pk,
            opponent_user=user,
        )
        self.pending_challenges[key] = pending
        pending.expiry_task = asyncio.create_task(
            self._expire_challenge(key, interaction.channel), name=f"battle-challenge-expiry-{id(pending)}"
        )

        await interaction.response.send_message(
            f"{user.mention}, you've been challenged to a battle by {interaction.user.mention} "
            f"using their **{card.description(short=True)}**!\n"
            f"Run `/battle accept` with your chosen card within "
            f"{CHALLENGE_EXPIRY // 60} minutes, or `/battle decline` to turn it down."
        )

    @app_commands.command()
    @app_commands.checks.bot_has_permissions(send_messages=True)
    async def accept(self, interaction: Interaction, card: BallInstanceTransform):
        """
        Accept a pending battle challenge with your chosen card.

        Parameters
        ----------
        card: BallInstanceTransform
            The card you want to fight with.
        """
        assert interaction.channel
        key = (interaction.channel.id, interaction.user.id)
        pending = self.pending_challenges.get(key)
        if pending is None:
            await interaction.response.send_message(
                "You don't have a pending challenge to accept in this channel.", ephemeral=True
            )
            return
        if card.player.discord_id != interaction.user.id:
            await interaction.response.send_message("You do not own that card.", ephemeral=True)
            return
        if await self.get_battle(interaction) is not None:
            self._clear_pending(key)
            await interaction.response.send_message("You already have an active battle.", ephemeral=True)
            return

        challenger_card = await BallInstance.objects.select_related("ball", "player").aget(
            pk=pending.challenger_card_id
        )
        # The challenger might have traded/released the card while the challenge was pending.
        if challenger_card.player.discord_id != pending.challenger_user.id:
            self._clear_pending(key)
            await interaction.response.send_message(
                "The challenger no longer owns the card they challenged with. Challenge cancelled.", ephemeral=True
            )
            return

        self._clear_pending(key)
        opponent_player, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)

        await self.start_battle(
            interaction,
            fighter1=(pending.challenger, pending.challenger_user, challenger_card),
            fighter2=(opponent_player, interaction.user, card),
        )

    @app_commands.command()
    async def decline(self, interaction: Interaction):
        """
        Decline a pending battle challenge.
        """
        assert interaction.channel
        key = (interaction.channel.id, interaction.user.id)
        pending = self.pending_challenges.get(key)
        if pending is None:
            await interaction.response.send_message("You don't have a pending challenge to decline.", ephemeral=True)
            return
        self._clear_pending(key)
        await interaction.response.send_message(
            f"{pending.challenger_user.mention}, {interaction.user.mention} declined your battle challenge."
        )

    async def start_battle(
        self,
        interaction: Interaction,
        fighter1: tuple[Player, discord.abc.User, BallInstance],
        fighter2: tuple[Player, discord.abc.User, BallInstance],
    ):
        assert interaction.channel
        battle = BattleInstance.configure(self, fighter1, fighter2)
        await interaction.response.send_message(view=battle)
        battle.message = await interaction.original_response()
        self.battles[interaction.channel.id][fighter1[1].id] = battle
        self.battles[interaction.channel.id][fighter2[1].id] = battle
        await battle.start_round()


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Battle(bot))

"""
This file contains the logic behind battles. It mirrors the structure of `ballsdex/packages/trade/trade.py`:
two main classes, `Combatant` and `BattleInstance`, act as data models, API functions, and Discord UI components
at the same time.

Ability lifecycle (per combatant, per ability)
-----------------------------------------------
LOCKED       -> ability is not currently usable. Either it's on cooldown, or it simply hasn't rolled yet.
ARMED        -> the trigger chance rolled successfully this round. The ability stays ARMED indefinitely across
                future rounds until the player either uses it or their card is eliminated. It does NOT expire on
                its own and does NOT re-roll while armed.
ON_COOLDOWN  -> the ability was just used. `cooldown_remaining` ticks down by 1 every round. Once it reaches 0,
                the ability returns to LOCKED and becomes eligible to roll again.

Trigger chance and cooldown length are per-card, configured via the `capacity_logic` JSON field on the Ball
model (see `Combatant._apply_ability_effect` for the full schema) rather than being fixed constants, so a
rarer/legendary card can be tuned to trigger more often or recharge faster than a common one.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord.ui import ActionRow, Button, Item, Section, Separator, TextDisplay, Thumbnail
from discord.utils import format_dt, utcnow

from ballsdex.core.discord import UNKNOWN_INTERACTION, Container, LayoutView
from bd_models.models import BallInstance, Player
from settings.models import settings

from .errors import AbilityNotArmedError, AbilityOnCooldownError, BattleError, BattleFinishedError

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

    from .cog import Battle as BattleCog

type Interaction = discord.Interaction["BallsDexBot"]

log = logging.getLogger(__name__)

BATTLE_TIMEOUT = 60 * 15
# How long combatants get to decide each round before the round auto-resolves as "held".
ROUND_DECISION_TIME = 20


class AbilityStatus(enum.Enum):
    LOCKED = "locked"
    ARMED = "armed"
    ON_COOLDOWN = "on_cooldown"


class Combatant(Container):
    """
    Represents one side of a battle. Also a [`Container`][discord.ui.Container], rendering that player's
    health bar, narration, and ability controls.

    Parameters
    ----------
    battle: BattleInstance
        The battle this combatant belongs to.
    player: Player
        The fetched player model of the user.
    user: discord.abc.User
        The Discord user model.
    countryball: BallInstance
        The card being fought with. Renamed conceptually to "member card" in a members-based fork, but the
        underlying model is left as `BallInstance` here since that's what this codebase currently provides.
    """

    def __init__(self, battle: "BattleInstance", player: Player, user: discord.abc.User, countryball: BallInstance):
        super().__init__()
        self.battle = battle
        self.cog = battle.cog
        self.player = player
        self.user = user
        self.countryball = countryball

        self.max_health = countryball.health
        self.current_health = countryball.health
        self.attack = countryball.attack

        # Ability state, configured per-card via `countryball.ball.capacity_logic`, e.g.:
        #   {"effect": "stun", "duration": 2, "trigger_chance": 0.15, "cooldown_rounds": 4}
        # `trigger_chance` and `cooldown_rounds` fall back to sane defaults if omitted, so existing
        # cards with only flavour text (no capacity_logic) still work as before.
        self.ability_name = countryball.ball.capacity_name
        self.ability_description = countryball.ball.capacity_description
        logic: dict = getattr(countryball.ball, "capacity_logic", None) or {}
        self.ability_trigger_chance: float = max(0.0, min(1.0, float(logic.get("trigger_chance", 0.20))))
        self.ability_cooldown_rounds: int = max(1, int(logic.get("cooldown_rounds", 3)))

        self.ability_status = AbilityStatus.LOCKED
        self.cooldown_remaining = 0
        # Whether this combatant has made their choice (use/hold) for the current round yet.
        self.decided_this_round = False

        # Status effects, driven by ability effects (see `apply_ability_effect`). All countdowns are
        # in rounds and are ticked down once per round in `tick_status_effects`.
        self.stunned_rounds = 0
        self.shield = 0
        self.attack_buff = 0
        self.attack_buff_rounds = 0

        self.view: "BattleInstance"

    def __repr__(self) -> str:
        return f"<Combatant player_id={self.player.pk} discord_id={self.user.id} hp={self.current_health}>"

    @property
    def is_alive(self) -> bool:
        return self.current_health > 0

    @property
    def effective_attack(self) -> int:
        return self.attack + self.attack_buff

    @property
    def awaiting_decision(self) -> bool:
        """
        `True` if this combatant's ability is armed and they haven't yet chosen use/hold this round.
        """
        return self.ability_status == AbilityStatus.ARMED and not self.decided_this_round

    async def interaction_check(self, interaction: Interaction) -> bool:
        if not await interaction.client.blacklist_check(interaction):
            return False
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your card to control!", ephemeral=True)
            return False
        return True

    # ==== API functions ====

    def apply_damage(self, amount: int):
        if self.shield > 0:
            absorbed = min(self.shield, amount)
            self.shield -= absorbed
            amount -= absorbed
        self.current_health = max(0, self.current_health - amount)

    def roll_ability_trigger(self):
        """
        Called once per round for a combatant whose ability is LOCKED. Rolls the trigger chance and,
        on success, arms the ability. Has no effect if the ability isn't currently LOCKED.
        """
        if self.ability_status != AbilityStatus.LOCKED:
            return
        if random.random() < self.ability_trigger_chance:
            self.ability_status = AbilityStatus.ARMED

    def tick_cooldown(self):
        """
        Called once per round for a combatant whose ability is ON_COOLDOWN.
        """
        if self.ability_status != AbilityStatus.ON_COOLDOWN:
            return
        self.cooldown_remaining -= 1
        if self.cooldown_remaining <= 0:
            self.cooldown_remaining = 0
            self.ability_status = AbilityStatus.LOCKED

    def use_ability(self, opponent: "Combatant") -> str:
        """
        Fires the armed ability against `opponent`, starting its cooldown.

        The actual effect is read from `countryball.ball.capacity_logic`, a JSON field with the shape::

            {
                "effect": "damage" | "heal" | "stun" | "buff_attack" | "shield",
                "value": int,
                "duration": int,
                "trigger_chance": float,   # 0.0-1.0, read at combatant setup, not here
                "cooldown_rounds": int     # read at combatant setup, not here
            }

        `value` means bonus damage dealt, HP restored, attack points gained, or shield points gained
        depending on `effect`. `duration` (in rounds) only applies to `stun` and `buff_attack`, and
        defaults to 1 if omitted. `trigger_chance` and `cooldown_rounds` are consumed once, in
        `Combatant.__init__`, to set `ability_trigger_chance`/`ability_cooldown_rounds` - they're
        listed here only for reference since they live in the same JSON blob. Unrecognised or missing
        `effect` values fall back to flavour-text-only.

        Returns
        -------
        str
            Narration text describing the ability's effect.

        Raises
        ------
        AbilityNotArmedError
            The ability isn't currently armed.
        """
        if self.ability_status != AbilityStatus.ARMED:
            raise AbilityNotArmedError()

        narration = self._apply_ability_effect(opponent)

        self.ability_status = AbilityStatus.ON_COOLDOWN
        self.cooldown_remaining = self.ability_cooldown_rounds
        self.decided_this_round = True
        return narration

    def _apply_ability_effect(self, opponent: "Combatant") -> str:
        logic: dict = getattr(self.countryball.ball, "capacity_logic", None) or {}
        effect = logic.get("effect")
        value = int(logic.get("value", 0))
        duration = int(logic.get("duration", 1))
        name = self.ability_name
        header = f"⚡ **{self.user.display_name}**'s *{name}* activates!"

        if effect == "damage":
            opponent.apply_damage(value)
            return f"{header} It deals {value} bonus damage to {opponent.user.display_name}'s card!"
        if effect == "heal":
            healed = min(self.max_health, self.current_health + value) - self.current_health
            self.current_health += healed
            return f"{header} It restores {healed} HP!"
        if effect == "stun":
            opponent.stunned_rounds = max(opponent.stunned_rounds, duration)
            return (
                f"{header} {opponent.user.display_name}'s card is stunned and will "
                f"miss their next {'attack' if duration == 1 else f'{duration} attacks'}!"
            )
        if effect == "buff_attack":
            self.attack_buff += value
            self.attack_buff_rounds = max(self.attack_buff_rounds, duration)
            return f"{header} Attack is boosted by {value} for {duration} round(s)!"
        if effect == "shield":
            self.shield += value
            return f"{header} A shield absorbing {value} damage forms around the card!"

        # No recognised effect configured yet - flavour text only, still consumes the charge.
        return f"{header} {self.ability_description}"

    def hold_ability(self):
        """
        Explicitly holds the armed ability for a future round. No cooldown is consumed and the ability
        stays ARMED indefinitely.
        """
        if self.ability_status != AbilityStatus.ARMED:
            raise AbilityNotArmedError()
        self.decided_this_round = True

    def tick_status_effects(self):
        """
        Called once per round for every living combatant, regardless of ability state, to count down
        stun and attack-buff durations set by `_apply_ability_effect`.
        """
        if self.stunned_rounds > 0:
            self.stunned_rounds -= 1
        if self.attack_buff_rounds > 0:
            self.attack_buff_rounds -= 1
            if self.attack_buff_rounds == 0:
                self.attack_buff = 0

    # ==== Display helpers ====

    def health_bar(self, segments: int = 10) -> str:
        filled = round((self.current_health / self.max_health) * segments) if self.max_health else 0
        filled = max(0, min(segments, filled))
        return "🟥" * filled + "⬜" * (segments - filled)

    def status_effects_line(self) -> str | None:
        parts = []
        if self.stunned_rounds > 0:
            parts.append(f"😵 Stunned ({self.stunned_rounds} round(s) left)")
        if self.shield > 0:
            parts.append(f"🛡️ Shielded ({self.shield} dmg)")
        if self.attack_buff > 0:
            parts.append(f"💪 +{self.attack_buff} ATK ({self.attack_buff_rounds} round(s) left)")
        return " · ".join(parts) if parts else None

    def ability_status_line(self) -> str:
        if self.ability_status == AbilityStatus.ARMED:
            return f"⚡ *{self.ability_name}* is **armed** — ready whenever you choose."
        if self.ability_status == AbilityStatus.ON_COOLDOWN:
            return f"🔒 *{self.ability_name}* recharging ({self.cooldown_remaining} round(s) left)."
        return f"*{self.ability_name}* not yet triggered."

    ability_row = ActionRow()

    @ability_row.button(label="Use Ability", style=discord.ButtonStyle.success)
    async def use_ability_button(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        opponent = self.battle.combatant2 if self is self.battle.combatant1 else self.battle.combatant1
        try:
            narration = self.use_ability(opponent)
        except BattleError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
            return
        self.battle.round_narration.append(narration)
        await self.battle.maybe_advance_round(interaction)

    @ability_row.button(label="Hold for Next Attack", style=discord.ButtonStyle.secondary)
    async def hold_button(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        try:
            self.hold_ability()
        except BattleError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
            return
        self.battle.round_narration.append(f"**{self.user.display_name}** holds their ability for now.")
        await self.battle.maybe_advance_round(interaction)

    async def refresh_container(self):
        """
        Rebuild this container's items with the current state, following the same rebuild-on-refresh
        pattern as `TradingUser.refresh_container` in trade.py.
        """
        self.clear_items()

        section = Section(
            TextDisplay(f"## {self.user.display_name}\n{self.health_bar()} {self.current_health}/{self.max_health}"),
            accessory=Thumbnail(self.user.display_avatar.url),
        )
        if not self.is_alive:
            self.accent_colour = discord.Colour.dark_grey()
            section.add_item(TextDisplay("This card has been knocked out."))
        elif self.ability_status == AbilityStatus.ARMED:
            self.accent_colour = discord.Colour.gold()
            section.add_item(TextDisplay(self.ability_status_line()))
        elif self.ability_status == AbilityStatus.ON_COOLDOWN:
            self.accent_colour = discord.Colour.blue()
            section.add_item(TextDisplay(self.ability_status_line()))
        else:
            self.accent_colour = discord.Colour.light_grey()
            section.add_item(TextDisplay(self.ability_status_line()))

        if self.is_alive and (status_line := self.status_effects_line()):
            section.add_item(TextDisplay(status_line))

        self.add_item(section)

        self.use_ability_button.disabled = not (self.is_alive and self.awaiting_decision and self.battle.active)
        self.hold_button.disabled = self.use_ability_button.disabled
        if self.is_alive and self.ability_status == AbilityStatus.ARMED:
            self.add_item(self.ability_row)


class BattleInstance(LayoutView):
    """
    A live battle. Also a [`LayoutView`][discord.ui.LayoutView].

    Attributes
    ----------
    combatant1: Combatant
        The first fighter, also a [`Container`][discord.ui.Container].
    combatant2: Combatant
        The second fighter, also a [`Container`][discord.ui.Container].
    message: discord.Message
        The message this view is attached to. Must be set immediately after sending.
    round_number: int
        The current round, starting at 1.
    """

    def __init__(self, cog: "BattleCog"):
        super().__init__(timeout=BATTLE_TIMEOUT)
        self.cog = cog
        self.combatant1: Combatant
        self.combatant2: Combatant
        self.message: discord.Message

        self.round_number = 1
        self.round_narration: list[str] = []
        self.winner: Combatant | None = None
        self._finished = False

        self.edit_lock = asyncio.Lock()
        self.round_timer_task: asyncio.Task | None = None

        self.battle_id = uuid.uuid4().hex
        self.timeout_task = asyncio.create_task(self._timeout(), name=f"battle-timeout-{id(self)}")

    async def on_error(self, interaction: Interaction, error: Exception, item: Item) -> None:
        if isinstance(error, discord.NotFound) and error.code in UNKNOWN_INTERACTION:
            log.warning("Expired interaction", exc_info=error)
            return
        log.exception(f"Error in battle between {self.combatant1} and {self.combatant2}", exc_info=error)
        await self.cleanup()
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("An error occurred, the battle has been cancelled.", ephemeral=True)

    async def _timeout(self):
        await asyncio.sleep(BATTLE_TIMEOUT)
        if self.active:
            await self.cleanup()

    @property
    def active(self) -> bool:
        return not self._finished and not self.is_finished()

    @classmethod
    def configure(
        cls,
        cog: "BattleCog",
        fighter1: tuple[Player, discord.abc.User, BallInstance],
        fighter2: tuple[Player, discord.abc.User, BallInstance],
    ):
        battle = cls(cog)
        battle.combatant1 = Combatant(battle, *fighter1)
        battle.combatant2 = Combatant(battle, *fighter2)
        battle.clear_items()
        battle.add_item(
            TextDisplay(
                f"## ⚔️ Battle — Round {battle.round_number}\n"
                f"{fighter2[1].mention} has been challenged by {fighter1[1].mention}!"
            )
        )
        battle.add_item(battle.combatant1)
        battle.add_item(Separator())
        battle.add_item(battle.combatant2)
        return battle

    # ==== Round resolution ====

    def _resolve_attacks(self):
        """
        Applies simultaneous attack damage for the round (respecting stun and attack buffs from
        abilities) and produces narration.
        """
        for attacker, defender in ((self.combatant1, self.combatant2), (self.combatant2, self.combatant1)):
            if not attacker.is_alive:
                continue
            if attacker.stunned_rounds > 0:
                self.round_narration.append(f"😵 **{attacker.user.display_name}**'s card is stunned and can't attack!")
                continue
            dmg = attacker.effective_attack
            defender.apply_damage(dmg)
            self.round_narration.append(f"**{attacker.user.display_name}**'s card strikes for {dmg} damage!")

    def _roll_and_tick_abilities(self):
        for combatant in (self.combatant1, self.combatant2):
            if not combatant.is_alive:
                continue
            combatant.tick_status_effects()
            if combatant.ability_status == AbilityStatus.ON_COOLDOWN:
                combatant.tick_cooldown()
            elif combatant.ability_status == AbilityStatus.LOCKED:
                combatant.roll_ability_trigger()
                if combatant.ability_status == AbilityStatus.ARMED:
                    self.round_narration.append(
                        f"⚡ **{combatant.user.display_name}**'s *{combatant.ability_name}* is ready to use!"
                    )
            combatant.decided_this_round = False

    def _check_winner(self) -> Combatant | None:
        if not self.combatant1.is_alive and not self.combatant2.is_alive:
            return None  # double knockout, handled separately as a draw
        if not self.combatant1.is_alive:
            return self.combatant2
        if not self.combatant2.is_alive:
            return self.combatant1
        return None

    async def start_round(self):
        """
        Resolves base attacks, rolls/ticks abilities, checks for a winner, and refreshes the message.
        Called at battle start and again every time both combatants have made their decision (or the
        per-round timer expires).
        """
        if not self.active:
            raise BattleFinishedError()

        self._resolve_attacks()

        winner = self._check_winner()
        if winner is not None or (not self.combatant1.is_alive and not self.combatant2.is_alive):
            self.winner = winner
            await self._finish(interaction=None)
            return

        self._roll_and_tick_abilities()
        self.round_number += 1
        await self._refresh_message(interaction=None)
        self._schedule_round_timer()

    def _schedule_round_timer(self):
        if self.round_timer_task and not self.round_timer_task.done():
            self.round_timer_task.cancel()
        self.round_timer_task = asyncio.create_task(
            self._round_timeout(), name=f"battle-round-timeout-{id(self)}-{self.round_number}"
        )

    async def _round_timeout(self):
        await asyncio.sleep(ROUND_DECISION_TIME)
        if not self.active:
            return
        # Anyone who hasn't decided is treated as holding automatically.
        for combatant in (self.combatant1, self.combatant2):
            if combatant.awaiting_decision:
                combatant.decided_this_round = True
                self.round_narration.append(f"**{combatant.user.display_name}** ran out of time and held.")
        await self.start_round()

    async def maybe_advance_round(self, interaction: Interaction | None):
        """
        Called after a combatant makes a use/hold decision. If both combatants (that are still armed
        and alive) have now decided, the round advances immediately instead of waiting for the timer.
        """
        pending = [c for c in (self.combatant1, self.combatant2) if c.awaiting_decision]
        if pending:
            await self._refresh_message(interaction)
            return
        if self.round_timer_task and not self.round_timer_task.done():
            self.round_timer_task.cancel()
        await self.start_round()
        if interaction is not None:
            await self._refresh_message(interaction)

    async def _refresh_message(self, interaction: Interaction | None):
        async with self.edit_lock:
            await self.combatant1.refresh_container()
            await self.combatant2.refresh_container()

            narration_text = "\n".join(self.round_narration[-6:]) if self.round_narration else ""
            header = self.children[0]
            assert isinstance(header, TextDisplay)
            header.content = f"## ⚔️ Battle — Round {self.round_number}\n{narration_text}"

            if not self.active:
                for item in self.walk_children():
                    if hasattr(item, "disabled"):
                        item.disabled = True  # type: ignore

            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            elif interaction is not None:
                await interaction.edit_original_response(view=self)
            else:
                await self.message.edit(view=self)

    async def _finish(self, interaction: Interaction | None):
        self._finished = True
        self.timeout_task.cancel()
        if self.round_timer_task:
            self.round_timer_task.cancel()
        self.stop()

        if self.winner is not None:
            loser = self.combatant2 if self.winner is self.combatant1 else self.combatant1
            self.round_narration.append(f"## 🏆 {self.winner.user.display_name} wins the battle!")
            log.info(
                f"Battle {self.battle_id} finished: winner={self.winner.player.pk} loser={loser.player.pk}"
            )
        else:
            self.round_narration.append("## Both cards were knocked out — it's a draw!")

        await self._refresh_message(interaction)

    async def cleanup(self):
        self._finished = True
        self.timeout_task.cancel()
        if self.round_timer_task:
            self.round_timer_task.cancel()
        self.stop()
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore
        timeout_note = TextDisplay(f"-# This battle timed out {format_dt(utcnow(), style='R')}.")
        self.add_item(timeout_note)
        await self.message.edit(view=self)

import logging

from settings.models import settings

log = logging.getLogger("ballsdex.packages.battle")


class BattleError(RuntimeError):
    """
    User-facing exceptions during a battle. Use `error_message` to obtain a friendly message.
    """

    msg: str | None = None

    @property
    def error_message(self) -> str:
        if self.msg is None:
            log.error("Unknown error during battle", exc_info=self)
            return "An unknown exception occurred. Contact support if this persists."
        return self.msg


class NotYourTurnError(BattleError):
    """
    Raised when a player tries to act on a round that isn't awaiting their decision.
    """

    msg = "It's not your turn to act right now."


class AbilityNotArmedError(BattleError):
    """
    Raised when a player tries to use an ability that isn't currently armed.
    """

    msg = "This ability isn't ready to use right now."


class AbilityOnCooldownError(BattleError):
    """
    Raised when a player tries to use an ability while it's still on cooldown.
    """

    msg = f"This {settings.collectible_name}'s ability is still on cooldown."


class BattleFinishedError(BattleError):
    """
    Raised when an action is attempted on a battle that has already ended.
    """

    msg = "This battle has already ended."


class AlreadyInBattleError(BattleError):
    """
    Raised when a player who is already in an active battle is challenged or tries to start another.
    """

    msg = "You are already in an active battle."

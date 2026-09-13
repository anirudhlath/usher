"""The "run `usher derive`" warning, said once per process rather than once per composed screen."""

from loguru import logger


class SaidOnce:
    """A latch around one log line, per instance and therefore per process.

    Not a decorator and not a module-level `set` of message strings: a set
    keyed on text would silently merge two providers whose wording converged,
    and would leak across tests in a way that makes a case pass because an
    *earlier* case already spoke. One latch per provider instance keeps the
    scope exactly as wide as the singleton that owns it, and a test that wants
    the warning back constructs a fresh provider — which is what every case
    here already does.
    """

    __slots__ = ("_said",)

    def __init__(self) -> None:
        self._said = False

    def warn(self, message: str) -> None:
        if self._said:
            return
        self._said = True
        logger.warning(message)


__all__ = ["SaidOnce"]

"""In-memory `Row` and `RowProvider`, for the composer's own arithmetic."""

from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import BuiltRow, DisplayHint, RowCard, RowFamily
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow


class FakeRow(Row):
    """A row that builds what it was told to build."""

    def __init__(
        self,
        slug: str,
        *,
        title: str | None = None,
        reason: str | None = None,
        family: RowFamily = RowFamily.SOURCE,
        display_hint: DisplayHint = DisplayHint.PORTRAIT,
        ttl: timedelta = timedelta(seconds=60),
        cards: Sequence[RowCard] = (),
    ) -> None:
        self._slug = slug
        self._title = title if title is not None else slug.replace("-", " ").title()
        self._reason = reason
        self._family = family
        self._display_hint = display_hint
        self._ttl = ttl
        self._cards = tuple(cards)
        self.builds = 0

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def title(self) -> str:
        return self._title

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def family(self) -> RowFamily:
        return self._family

    @property
    def display_hint(self) -> DisplayHint:
        return self._display_hint

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    async def build(self, ctx: RowContext) -> BuiltRow:
        self.builds += 1
        return BuiltRow(
            slug=self._slug,
            title=self._title,
            reason=self._reason,
            family=self._family,
            display_hint=self._display_hint,
            ttl=self._ttl,
            cards=self._cards,
        )


class FakeRowProvider(RowProvider):
    """Proposes exactly what it was constructed with -- including nothing.

    `rows` is the rows it proposed, in order, so a composer case can assert
    `provider.rows[0].builds == 0` -- which is the only way to see the
    two-phase property from outside: a one-phase composer that builds
    everything and then ranks passes every ordering assertion.
    """

    def __init__(self, *, proposals: Sequence[ScoredRow] = (), slug_prefix: str = "fake") -> None:
        self._proposals = tuple(proposals)
        self._slug_prefix = slug_prefix
        self.rows: tuple[Row, ...] = tuple(proposal.row for proposal in self._proposals)
        self.contexts: list[RowContext] = []

    @property
    def slug_prefix(self) -> str:
        return self._slug_prefix

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        self.contexts.append(ctx)
        return self._proposals

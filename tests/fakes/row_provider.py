"""In-memory `Row` and `RowProvider`, for the composer's own arithmetic.

Both stand in for the nine real providers Group H builds, and both are
deliberately *incapable* of the thing those providers are hard to get right:
they hold no signal, read nothing from the context, and propose exactly what
they were constructed with. That is the point. A composer test wants to fix
the proposals and vary the composer; a provider test wants the opposite, and
uses the real provider with a seeded distractor.

**Where this is more forgiving than a real provider, on purpose. Three.**

1. **`propose` never queries**, so nothing here can exercise the failure the
   milestone is about -- a provider that, finding no signal, returns something
   generic. `FakeRowProvider(proposals=())` asserts only that empty is a
   legal, non-exceptional answer. The guarantee is nine per-provider cases.
2. **`build` never hydrates.** It returns the cards it was given, so a
   `hydrate()` that loses the progress pair, or that drops unowned titles, is
   invisible here and is `services/rows/base.py`'s to pin.
3. **It cannot disagree with itself.** A real provider's `propose` and
   `build` are two methods reading the same signal at two instants, and
   either can be the stricter -- a looser `propose` yields an empty row, a
   stricter one silently suppresses a row that would have been fine. This
   fake builds whatever it proposed, by construction.

`contexts` records every context `propose` was handed, so Group I can assert
the composer called each provider exactly once per screen rather than once per
proposal.
"""

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

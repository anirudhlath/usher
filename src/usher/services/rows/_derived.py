"""The "run `usher derive`" warning, said once per process rather than once
per composed screen.

**This is CLAUDE.md's "a per-process fact logged in a per-pass function"
finding, arriving in the row layer.** M5 measured it at `build_worker`, which
logged "no TMDb API key configured" once per worker pass at a 5 s poll:
~17,280 warnings a day. Three providers here — `because_you_watched`,
`franchise` and `people` — each said "run `usher derive`" from inside
`propose`, which runs once per composed home screen; at the 30 s screen TTL
that is ~2,880 screens a day per household and ~8,640 warnings a day between
them, on a **fresh install**, which is the deployment least able to tell a
real warning from noise.

The information is genuinely useful and is not being dropped. PRD 08's
degradation rule is that a narrowed deployment must be *visible*: a provider
that silently never fires is indistinguishable from a household with thin
history, which is precisely the "renders identically to a right one" failure
this milestone is about.

**Why a one-shot here rather than M5's fix exactly.** M5 did not add a guard;
it *moved* the line to `composition.metadata_provider`, a function each
composition root calls exactly once per process. That worked because "is a
TMDb key configured" is a fact about `Settings`, available with no I/O.

There is no counterpart here, and inventing one would be worse than this.
"Has `usher derive` run" is a fact about the **database**, so a composition
root could only learn it by opening a connection and issuing a `count(*)` at
start-up — and this project has already refused exactly that shape, twice, for
exactly the right reasons: `create_app`'s lifespan builds an engine and opens
no connection, which is what keeps `/health` answering 200 with Postgres down,
and `ensure_default_user` was made a request-scoped dependency rather than a
lifespan call because "a write at startup turns a database outage into a crash
loop and an unmigrated schema into a failure to boot".

So the decision stays where it is made — inside `propose`, where the count is
already being read for its own sake — and what moves is the **rate**: from once
per pass to once per process, which is the rate at which the fact is worth
saying. The providers are module-level singletons built by
`row_providers(*, semantic)`, so a per-instance latch *is* a per-process latch.

**What this costs, said rather than hidden.** An operator who runs `usher
derive` while a long-lived server is up gets no "it is fixed now" line; they
get rows, which is the better signal. And an operator who starts a server,
sees the warning scroll past, and looks again an hour later will not find it
repeated — so the line names the command to run rather than merely describing
the state, and the same fact is available on demand from `usher derive`'s own
report. A warning that is true once is worth more than a warning that is true
2,880 times and read zero.
"""

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

"""The suggest surface, against a stub index rather than a database.

What is asserted here is the *shape*: one ranking per case in case order,
empty rankings preserved, strata derived from the case and not re-joined.
Driving the real `SearchService` is `tests/integration/`'s.
"""

import uuid

from usher.eval.goldens.suggest import TypoCase
from usher.eval.surfaces.suggest import SurfaceRun, rank_cases


class _StubSuggester:
    """Answers a fixed mapping; records what it was asked, in order."""

    def __init__(self, answers: dict[str, list[uuid.UUID]]) -> None:
        self._answers = answers
        self.asked: list[str] = []

    async def __call__(self, probe: str, limit: int) -> list[uuid.UUID]:
        self.asked.append(probe)
        return self._answers.get(probe, [])[:limit]


def _case(name: str, probe: str, band: str = "5-7", klass: str = "substitution") -> TypoCase:
    return TypoCase(
        title_id=uuid.UUID(int=abs(hash(name)) % (2**32)),
        name=name,
        band=band,
        typo_class=klass,
        probe=probe,
    )


async def test_every_case_gets_a_ranking_even_when_nothing_came_back() -> None:
    """The denominator is the case count. A surface that emitted rankings
    only for cases that matched would report recall over the cases that
    worked, which rises as the index gets worse."""
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run: SurfaceRun = await rank_cases(
        cases, _StubSuggester({"Alein": [cases[0].title_id]}), limit=5
    )
    assert len(run.rankings) == len(cases)
    assert run.rankings[1].ranked_ids == ()


async def test_the_ranking_order_is_the_index_order() -> None:
    """`suggest` is not re-ranked by the service (both tiers order their own
    answer), so the eval must not reorder it either -- MRR is the metric that
    would silently change if it did."""
    case = _case("Alien", "Alein")
    other = uuid.UUID(int=99)
    run = await rank_cases((case,), _StubSuggester({"Alein": [other, case.title_id]}), limit=5)
    assert run.rankings[0].ranked_ids == (str(other), str(case.title_id))


async def test_the_probe_is_what_reaches_the_index_not_the_name() -> None:
    """The whole measurement is that a *misspelt* prefix still finds the
    title. An eval that sent the correct name would score ~1.0 on any index
    and prove nothing."""
    suggester = _StubSuggester({})
    await rank_cases((_case("Alien", "Alein"),), suggester, limit=5)
    assert suggester.asked == ["Alein"]


async def test_a_latency_is_recorded_for_every_case() -> None:
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert len(run.latencies_ms) == len(cases)
    assert all(one >= 0.0 for one in run.latencies_ms)


async def test_the_relevant_map_is_one_entry_per_case() -> None:
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert set(run.relevant) == {case.query_id for case in cases}


async def test_strata_split_by_band_and_by_typo_class_and_never_average_them() -> None:
    """ADR-0031 ships two tiers with very different profiles and ADR-0002
    measured 0.0% on one typo class against 95%+ on a long band. A mean over
    either dimension describes neither."""
    cases = (
        _case("Up", "Uq", band="2-4", klass="substitution"),
        _case("Aliens", "Alines", band="5-7", klass="transposition"),
    )
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert run.strata_for(cases[0].query_id) == ("all", "band=2-4", "typo_class=substitution")


class _Boom:
    async def __call__(self, probe: str, limit: int) -> list[uuid.UUID]:
        raise RuntimeError("index is down")


async def test_an_index_that_raises_is_not_scored_as_a_miss() -> None:
    """A zero and a failure are different facts and only one of them is a
    regression. Swallowing the error would report the outage as a quality
    collapse and send somebody to read the ranking code."""
    import pytest

    with pytest.raises(RuntimeError, match="index is down"):
        await rank_cases((_case("Alien", "Alein"),), _Boom(), limit=5)

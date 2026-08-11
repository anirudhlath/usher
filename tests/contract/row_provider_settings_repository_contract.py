"""Behaviour every `RowProviderSettingsRepository` implementation must satisfy.

Two methods, and almost the whole contract is one decision:
**`overrides()` must never manufacture a `False` for a slug nobody has
touched.** The trap this suite exists to catch is a mirror of the registry
disguised as a map of overrides -- correctly shaped, populated, and
indistinguishable from a working toggle right up until an operator disables
one provider and reads every other one as disabled too.

Subclass and provide a `repository` fixture:

    class TestFakeRowProviderSettingsRepository(RowProviderSettingsRepositoryContract):
        @pytest.fixture
        def repository(self) -> FakeRowProviderSettingsRepository:
            return FakeRowProviderSettingsRepository()
"""

from usher.ports.repository import RowProviderSettingsRepository


class RowProviderSettingsRepositoryContract:
    async def test_a_freshly_created_table_has_no_overrides_at_all(
        self, repository: RowProviderSettingsRepository
    ) -> None:
        assert await repository.overrides() == {}

    async def test_a_slug_that_has_never_been_set_is_absent_rather_than_false(
        self, repository: RowProviderSettingsRepository
    ) -> None:
        """The whole port in one assertion: an untouched provider's slug is
        not a key in `overrides()` at all, so a caller must not
        `.get(slug, False)` its way to treating "never configured" as
        "explicitly disabled" -- the two are different operator actions and
        the entire reason this table is not the registry.

        This is the weak red on its own (an empty repository trivially has no
        keys at all); `test_disabling_one_slug_leaves_the_other_nine_absent_
        and_re_enabling_it_removes_nothing` below is the one with teeth,
        because it asks the same question after a write has actually
        happened.
        """
        overrides = await repository.overrides()

        assert "curated" not in overrides

    async def test_disabling_one_slug_leaves_the_other_nine_absent_and_re_enabling_it_removes_nothing(  # noqa: E501
        self, repository: RowProviderSettingsRepository
    ) -> None:
        """**The red with teeth.** An `overrides()` that returns `{slug:
        False}` for every slug it has ever been asked about, rather than only
        the ones actually stored, satisfies a bare membership check and fails
        this one: after touching exactly one slug, the map holds exactly one
        entry, and every one of the other nine providers this milestone ships
        -- untouched -- is absent rather than reading as disabled.

        And re-enabling that one slug is not a delete. The table holds
        *overrides*, not a list of disabled providers, so setting it back to
        `True` is still a recorded operator action -- kept exactly as a
        `False` one would be -- rather than a row that vanishes back into
        "never configured". Nothing about any other slug moves either way.
        """
        await repository.set_enabled("curated", enabled=False)
        assert await repository.overrides() == {"curated": False}

        await repository.set_enabled("curated", enabled=True)

        assert await repository.overrides() == {"curated": True}

    async def test_two_slugs_are_independent(
        self, repository: RowProviderSettingsRepository
    ) -> None:
        """Rules out a single-slot implementation that remembers only the
        most recently touched provider -- plausible if `set_enabled` were
        written before `overrides()` and tested by re-reading the one slug
        just written."""
        await repository.set_enabled("curated", enabled=False)
        await repository.set_enabled("seasonal", enabled=True)

        overrides = await repository.overrides()

        assert overrides == {"curated": False, "seasonal": True}

    async def test_setting_the_same_slug_twice_upserts_rather_than_duplicating(
        self, repository: RowProviderSettingsRepository
    ) -> None:
        """`slug_prefix` is the primary key, so a second write for the same
        slug must replace rather than duplicate. An implementation that plain
        `INSERT`s raises a primary-key conflict on the second call instead of
        completing -- the failure worth having, since an operator flipping a
        provider twice in one session (or a route retried after a timeout) is
        ordinary rather than exceptional.

        `ON CONFLICT (slug_prefix) DO UPDATE` is the Postgres mechanism; this
        case is what it has to accomplish, and it fails on the Postgres arm
        without that clause -- with a raised exception, not a wrong answer.
        """
        await repository.set_enabled("curated", enabled=False)
        await repository.set_enabled("curated", enabled=False)

        assert await repository.overrides() == {"curated": False}

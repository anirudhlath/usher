from usher.db.migrations.status import code_head_revision


def test_code_head_revision_matches_the_single_shipped_migration() -> None:
    """No Docker needed: reads usher/db/migrations/versions/*.py directly
    off disk, the same files `alembic upgrade head` itself would use --
    doesn't touch a database at all. Pinned to the literal revision id
    (not just "is not None") so a second migration ever added without
    updating this test fails loudly here instead of silently changing
    what "the" expected head means.
    """
    assert code_head_revision() == "a8a0e10ff464"

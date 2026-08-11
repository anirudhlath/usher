"""IMDb `name.basics` x `title.principals` -> `titles.credit_names`.

No network, no Docker, no real dataset file. Third sibling of
`test_adapters_bulk_imdb.py` (`title.basics`/`title.ratings`) and
`test_adapters_bulk_imdb_akas.py` (`title.akas`).

**This file exists because T3's refusal was of a *design*, not of the two
files.** M9's T3 measured the `people` + `credits` entity design at
**2,701,697,024 B (2.702 GB) against a 2.0 GB ceiling** and it was refused, so
nothing here materialises a person or a credit row. What survives the refusal
is the **name text**, which is what weight class B of `search_document`
actually indexes -- so the two parsers exist and their only consumer is a
direct fill of `titles.credit_names`.

**One dataset over two files, and that is the whole reason
`IMDbCreditNamesDataset` is not two `BulkDataset`s.** A credit name is a join:
`title.principals` carries `(tconst, ordering, nconst)` and only
`name.basics` knows what an `nconst` is called. With no `people` table there
is nowhere in the database to put the right-hand side of that join, so it is
resolved in the adapter -- against a measured, memory-bounded index rather
than a `dict[str, str]`.
"""

import csv
import gzip
import io
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.imdb import (
    IMDbCreditNamesDataset,
    ImdbNameIndex,
    parse_names_row,
    parse_principals_row,
)
from usher.ports.bulk import BulkCursor, ImdbName
from usher.ports.errors import PortDataMalformed

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


def _names_lines() -> list[str]:
    return (_FIXTURES / "name.basics.slice.tsv").read_text(encoding="utf-8").splitlines()


def _principals_lines() -> list[str]:
    return (_FIXTURES / "title.principals.slice.tsv").read_text(encoding="utf-8").splitlines()


def _index() -> ImdbNameIndex:
    index = ImdbNameIndex()
    for line in _names_lines():
        row = parse_names_row(line)
        if row is not None:
            index.add(row)
    return index


def _stage(tmp_path: Path) -> Path:
    """gzip both committed slices into a scratch cache directory, so the
    adapter reads exactly the shape it reads in production."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    for source, name in (
        ("name.basics.slice.tsv", "name.basics.tsv.gz"),
        ("title.principals.slice.tsv", "title.principals.tsv.gz"),
    ):
        (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def _local(cache: Path, *, etags: dict[str, str] | None = None) -> httpx.MockTransport:
    """Serves whatever is already in `cache`, so `ensure_local` short-circuits
    on the revision stamp and no bytes are ever transferred.

    `etags` lets a case give the two files *different* revisions, which is the
    state the composite revision exists for: IMDb does not regenerate the
    seven dumps together.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        etag = (etags or {}).get(name, '"fixture"')
        (cache / f"{name}.revision").write_text(etag)
        return httpx.Response(200, content=(cache / name).read_bytes(), headers={"etag": etag})

    return httpx.MockTransport(handler)


def _dataset(
    cache: Path, *, batch_size: int, etags: dict[str, str] | None = None
) -> IMDbCreditNamesDataset:
    client = httpx.AsyncClient(transport=_local(cache, etags=etags))
    return IMDbCreditNamesDataset(client, cache, batch_size=batch_size)


# --- name.basics ------------------------------------------------------


def test_the_name_basics_header_is_filtered_not_parsed() -> None:
    assert parse_names_row(_names_lines()[0]) is None


def test_a_name_basics_row_carries_the_nconst_and_the_primary_name() -> None:
    row = parse_names_row(_names_lines()[1])
    assert row is not None
    assert (row.imdb_id, row.name) == ("nm99000010", "Ada Synthetic")


def test_a_person_with_no_primary_name_is_dropped_rather_than_stored_empty() -> None:
    r"""`\N` in `primaryName` is IMDb's null, and there are **89 of them**
    among the 15,563,615 data rows of the pinned `name.basics.tsv.gz`
    (`"a3b9681921c92e5917182d1ecc05bd2d-37"`, measured 2026-08-11).

    Storing the empty string instead would put a *searchable* empty lexeme
    into weight class B for every title that person is credited on -- the
    same argument `parse_akas_row` makes for
    `ck_title_search_names_name_not_empty` one file over, arriving at a
    column that has no such CHECK to lean on.
    """
    assert parse_names_row(_names_lines()[4]) is None


def test_a_name_preserves_an_embedded_double_quote() -> None:
    """The finding that rules out the csv module, re-confirmed on a third file.

    IMDb's TSVs have no quoting mechanism, so `primaryName` may open *and*
    close with a literal `"` and `csv.reader`'s default `QUOTE_MINIMAL` then
    strips both, silently. Measured over the whole pinned `name.basics.tsv.gz`:
    **138 names carry a `"` and 7 of them open with one.** Small next to
    `title.akas`' 39,880 -- and the seven are people whose names would be
    rewritten on every title they are credited on.

    Asserted against `csv.reader` in the same case rather than only against
    the parser: a case that checks the parser alone passes just as well when
    the trap has quietly stopped existing.
    """
    line = _names_lines()[2]
    row = parse_names_row(line)
    assert row is not None
    assert row.name == '"Bo Synthetic"'
    stripped = next(csv.reader(io.StringIO(line), delimiter="\t"))
    assert stripped[1] == "Bo Synthetic", "the csv trap this parser exists to avoid"


def test_a_name_basics_row_with_the_wrong_column_count_is_malformed() -> None:
    """Six columns -- `nconst primaryName birthYear deathYear
    primaryProfession knownForTitles` -- taken from the real header at the
    pinned snapshot, not from IMDb's published schema. Measured over all
    15,563,615 data rows: **zero** split to any other count.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_names_row("nm99000010\tAda Synthetic\t1970")
    assert exc_info.value.detail == "nm99000010"


def test_an_nconst_that_is_not_an_nconst_is_malformed_rather_than_skipped() -> None:
    """The index is direct-addressed on the integer inside the id, so a
    person id that is not `nm` + digits has nowhere to go. Measured: **0 of
    15,563,615** rows carry one, so a row that does is an upstream format
    change and stopping is the honest response -- same reasoning as
    `parse_akas_row`'s non-integer `ordering`.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_names_row("xx99000010\tAda Synthetic\t\\N\t\\N\t\\N\t\\N")
    assert exc_info.value.detail == "xx99000010.nconst"


# --- title.principals -------------------------------------------------


def test_the_principals_header_is_filtered_not_parsed() -> None:
    assert parse_principals_row(_principals_lines()[0]) is None


def test_a_principals_row_carries_the_title_the_person_and_the_ordering() -> None:
    row = parse_principals_row(_principals_lines()[1])
    assert row is not None
    assert (row.imdb_id, row.ordering, row.person_imdb_id) == ("tt99000020", 1, "nm99000010")


def test_a_principals_row_with_the_wrong_column_count_is_malformed() -> None:
    """Six columns -- `tconst ordering nconst category job characters` --
    measured over all 101,151,422 data rows of the pinned
    `title.principals.tsv.gz` (`"08ce60665889cb40c7371e1eab44a1f2-93"`):
    **zero** split to any other count.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_principals_row("tt99000020\t1\tnm99000010")
    assert exc_info.value.detail == "tt99000020"


def test_a_principals_row_with_no_usable_ordering_is_malformed() -> None:
    r"""`ordering` is the only per-title ranking the dump supplies, so a `\N`
    or a non-integer is a format change rather than a row to guess at.
    Measured: present and integral on all 101,151,422 rows, min 1, max 75.
    """
    for ordering in (r"\N", "first"):
        with pytest.raises(PortDataMalformed) as exc_info:
            parse_principals_row(f"tt99000020\t{ordering}\tnm99000010\tactor\t\\N\t\\N")
        assert exc_info.value.detail == "tt99000020.ordering"


def test_no_row_is_filtered_on_its_category() -> None:
    """13 categories in the pinned dump -- `actor` (23,895,326) through
    `archive_sound` (13,782) -- and **none is filtered**, deliberately.

    IMDb already caps the file at its own editorial selection of principals
    (max 75 rows for one title, mean 8.8), which is the same shape
    `services/derive._credit_names` produces from TMDb by taking the top ten
    billed and every stored crew name. A retain-list here would silently drop
    whatever category IMDb adds next; `archive_footage` is the only one that
    reads like noise and it is 0.6% of the file.
    """
    row = parse_principals_row(_principals_lines()[9])
    assert row is not None
    assert (row.imdb_id, row.person_imdb_id) == ("tt99000040", "nm99000060")


# --- the index --------------------------------------------------------


def test_the_index_answers_a_name_for_an_nconst_it_holds_and_none_otherwise() -> None:
    index = _index()
    assert index.get("nm99000010") == "Ada Synthetic"
    assert index.get("nm99000060") is None


def test_the_index_is_sparse_rather_than_dense_in_the_ids_it_is_given() -> None:
    """`name.basics` is sorted **lexicographically by the `nconst` string**,
    not numerically -- an eight-digit id sharing a seven-digit id's first
    seven characters sorts before it, because the comparison never reaches
    the length. Measured, **738,680 descents** in the integer sequence at the
    pinned snapshot. A structure that assumed ascending integers and bisected
    would answer `None` for millions of real people, and every one of those
    is a title quietly losing a name.

    So the index direct-addresses instead, and this case pins both halves of
    that: a large id arriving before a small one is answerable in both
    directions, **and it does not cost its own address space.** The second
    assertion is not tidiness -- a flat table sized by the largest id
    allocates 396 MB for these two rows, because this project's reserved
    synthetic band (`nm99\\d{6}`) starts at 99,000,000, and every case in this
    file paid it before the table was chunked.
    """
    index = ImdbNameIndex()
    index.add(ImdbName(imdb_id="nm99000090", name="Late Synthetic"))
    index.add(ImdbName(imdb_id="nm99000010", name="Early Synthetic"))
    assert index.get("nm99000090") == "Late Synthetic"
    assert index.get("nm99000010") == "Early Synthetic"
    assert len(index) == 2
    assert index.nbytes < 1_000_000, "two ids must not cost their own id space"


def test_the_index_reports_what_it_costs() -> None:
    """The whole of `name.basics` fits in **361,703,752 B (345 MiB)** and
    builds in **19.5 s** (measured 2026-08-11 on the pinned snapshot, peak RSS
    361.3 MB). That is the price of doing this join with no `people` table,
    and an importer that cannot report it cannot be held to it.
    """
    index = _index()
    assert index.nbytes > 0
    assert len(index) == 4


# --- the dataset ------------------------------------------------------


async def test_the_dataset_names_itself_and_carries_imdbs_attribution(tmp_path: Path) -> None:
    dataset = _dataset(_stage(tmp_path), batch_size=10)
    assert dataset.name == "imdb.credit_names"
    assert "IMDb" in dataset.attribution


async def test_the_revision_pins_both_files_because_they_are_not_one_snapshot(
    tmp_path: Path,
) -> None:
    """**The seven IMDb files are not one snapshot**, and this dataset reads
    two of them. Measured at the T3 pin: `name.basics` was regenerated
    2026-08-10 12:53:46 GMT and `title.principals` 2026-08-11 00:48:34 GMT,
    and **3,734 distinct `nconst` values (7,701 rows) named by
    `title.principals` are in no `name.basics` row at all.**

    A single-file revision would therefore call two genuinely different pairs
    of files the same snapshot, and a stored cursor from one would be replayed
    against the other. The composite names both files and both values, so it
    changes when *either* side moves.
    """
    cache = _stage(tmp_path)
    etags = {"name.basics.tsv.gz": '"names-1"', "title.principals.tsv.gz": '"principals-9"'}
    revision = await _dataset(cache, batch_size=10, etags=etags).revision()
    assert "name.basics" in revision
    assert "title.principals" in revision
    assert '"names-1"' in revision
    assert '"principals-9"' in revision


async def test_the_cursor_carries_the_same_revision_the_dataset_reports(tmp_path: Path) -> None:
    """`BootstrapService` stores `revision()`'s answer and the cursor
    together, and the next run compares the stored cursor's revision against
    a fresh `revision()`. A cursor stamped with anything else is a checkpoint
    that can never match — every run would discard it and re-read all
    101,151,422 lines, silently, because a discarded cursor is also what a
    legitimately-moved snapshot looks like.

    **Written because a mutation survived without it.** Computing the
    batches' revision from `title.principals` alone left every case in this
    file green: `revision()` has its own code path and its own case, and
    nothing compared the two. The etags here are deliberately different per
    file so a single-file spelling cannot coincide with the composite.
    """
    cache = _stage(tmp_path)
    etags = {"name.basics.tsv.gz": '"names-1"', "title.principals.tsv.gz": '"principals-9"'}
    dataset = _dataset(cache, batch_size=10, etags=etags)
    reported = await dataset.revision()
    assert '"names-1"' in reported, "the premise: the two files carry different revisions"

    batches = [batch async for batch in dataset.batches()]

    assert [batch.cursor.revision for batch in batches] == [reported]


async def test_a_title_yields_one_row_carrying_its_names_in_imdbs_own_ordering(
    tmp_path: Path,
) -> None:
    """`ordering` is the ranking and it is applied, not assumed.

    The premise this case rests on is that the fixture's `tt99000030` rows are
    **not** in `ordering` order in the file, so a parser that kept file order
    answers `("Dee Synthetic", "Cyd Synthetic")` and one that sorts answers the
    reverse. Asserted below rather than trusted: the real dump *is* ordered
    within every title (measured over all 101,151,422 rows), so the sort is
    unobservable against production data and only a deliberately disordered
    fixture can see it.
    """
    orderings = [
        (row.imdb_id, row.ordering)
        for row in map(parse_principals_row, _principals_lines())
        if row is not None and row.imdb_id == "tt99000030"
    ]
    assert orderings != sorted(orderings, key=lambda one: one[1]), (
        "the premise: the fixture's second title is out of ordering order in the file"
    )

    dataset = _dataset(_stage(tmp_path), batch_size=10)
    rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert [(row.imdb_id, row.names) for row in rows] == [
        ("tt99000020", ("Ada Synthetic", '"Bo Synthetic"', "Cyd Synthetic")),
        ("tt99000030", ("Cyd Synthetic", "Dee Synthetic")),
    ]


async def test_a_person_credited_twice_on_one_title_contributes_one_name(
    tmp_path: Path,
) -> None:
    """**9,404,442 of the 101,151,422 principal rows repeat a person already
    credited on the same title** -- a director who also wrote it, an actor who
    also produced. Measured at the pin.

    Repeating the name inflates its term frequency in a tsvector for no reason
    a searcher would recognise, which is the identical argument
    `services/derive._credit_names` makes for the TMDb-derived path. First
    position is kept, so the ranking is the person's *best* billing.
    """
    dataset = _dataset(_stage(tmp_path), batch_size=10)
    rows = {
        row.imdb_id: row.names
        for batch in [b async for b in dataset.batches()]
        for row in batch.rows
    }
    assert rows["tt99000020"].count("Cyd Synthetic") == 1


async def test_a_principal_naming_a_person_no_name_basics_row_holds_is_dropped(
    tmp_path: Path,
) -> None:
    """The two files are not one snapshot, so a dangling `nconst` is expected
    rather than exceptional -- **3,734 of them, over 7,701 rows**, at the T3
    pin. Dropping the row is the only available answer: there is no name to
    write and no `people` row to point at.

    **A title whose principals all dangle yields no row at all**, which is the
    half that matters: an empty `names` tuple would reach the writer and blank
    a `credit_names` some other source had filled. 156 titles in the pinned
    dump are in exactly that state.
    """
    dataset = _dataset(_stage(tmp_path), batch_size=10)
    rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert [row.imdb_id for row in rows] == ["tt99000020", "tt99000030"]
    assert all(row.names for row in rows)


async def test_a_titles_names_are_never_split_across_two_batches(tmp_path: Path) -> None:
    """A batch is a transaction and the write is a **set**, not an append: a
    title split across two batches would have its array overwritten by the
    second half, silently losing the names in the first.

    `batch_size=1` with a five-row title is the shape that would do it.
    """
    dataset = _dataset(_stage(tmp_path), batch_size=1)
    batches = [batch async for batch in dataset.batches()]
    assert [len(batch.rows) for batch in batches] == [1, 1]
    assert batches[0].rows[0].names == ("Ada Synthetic", '"Bo Synthetic"', "Cyd Synthetic")


async def test_the_cursor_advances_to_a_title_boundary_and_a_resume_loses_nothing(
    tmp_path: Path,
) -> None:
    """`position` counts **lines consumed through the end of the last
    completed title**, not lines read -- the boundary is detected by reading
    the first line of the *next* title, and a cursor pointing past it would
    resume mid-title and write a partial name list.

    The premise: the first batch's cursor must stop **before** the second
    title's first line. The slice is a header plus five `tt99000020` rows, so
    that boundary is 6 lines consumed and the next line read is the 7th --
    which is the off-by-one this assertion caught in its own first draft.
    """
    cache = _stage(tmp_path)
    batches = [batch async for batch in _dataset(cache, batch_size=1).batches()]
    assert batches[0].cursor.position == 6, "the premise: the boundary is the title's last line"
    assert batches[0].cursor.rows_seen == 1

    resumed = [
        row
        async for batch in _dataset(cache, batch_size=10).batches(resume_from=batches[0].cursor)
        for row in batch.rows
    ]
    assert [row.imdb_id for row in resumed] == ["tt99000030"]


async def test_a_cursor_from_another_snapshot_is_discarded(tmp_path: Path) -> None:
    """Line N of yesterday's dump is not line N of today's, and here it is
    two dumps that can move independently. Every write downstream is a set
    over a whole title, so restarting is slow rather than wrong.
    """
    cache = _stage(tmp_path)
    stale = BulkCursor(revision="an-invented-older-pin", position=5, rows_seen=1)
    rows = [
        row
        async for batch in _dataset(cache, batch_size=10).batches(resume_from=stale)
        for row in batch.rows
    ]
    assert [row.imdb_id for row in rows] == ["tt99000020", "tt99000030"]


async def test_a_malformed_row_raises_through_batches_instead_of_truncating(
    tmp_path: Path,
) -> None:
    """A short read and a clean end of file are indistinguishable to a caller
    that only sees batches, so the parser's refusal has to survive the
    generator rather than ending it.
    """
    cache = _stage(tmp_path)
    (cache / "title.principals.tsv.gz").write_bytes(
        gzip.compress(b"tconst\tordering\tnconst\tcategory\tjob\tcharacters\ntt99000020\tone\n")
    )
    with pytest.raises(PortDataMalformed):
        async for _ in _dataset(cache, batch_size=10).batches():
            pass

"""Unit tests for the dispatch-target value object.

``services/dispatch_target.py`` is the ONE discriminator over "where may this unit
run?" and the ONE codec for the ``target_printer_ids`` column, so what is pinned here
is the total-ness of both: every row shape resolves to exactly one of four kinds, and
every stored value — including the ones nothing in this fork writes — decodes without
raising.

Two of these tests exist because the alternative is a silent drift rather than a
failure:

* ``TestPrinterFilterAgreesWithMatches`` drives the Python predicate and the SQL
  predicate over the SAME seeded printers and asserts they select the same ids.
  The deep-park's Python model comparison and the scheduler's SQL one used to be two
  hand-kept mirrors (no SQL-side normalisation existed to keep them honest); this
  object carries both spellings, so the drift is testable
  here instead of only being avoidable.
* ``TestForRun`` pins the model normalisation as a MIRROR of
  ``production_run.create_production_run`` — a run's stored ``target_model`` and this
  object's must be the same string, warts included.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.services.dispatch_target import (
    DispatchTarget,
    TargetKind,
    decode_printer_ids,
    encode_printer_ids,
    target_of,
)

pytestmark = pytest.mark.unit


class _Row:
    """A queue row's three target columns and nothing else — the ``HasTargetColumns`` shape.

    Deliberately not a ``PrintQueueItem``: ``target_of`` reads three attributes, and
    binding the test to the ORM row would hide the fact that the Protocol is what it
    actually depends on.
    """

    def __init__(
        self,
        *,
        printer_id: int | None = None,
        target_model: str | None = None,
        target_printer_ids: str | None = None,
    ) -> None:
        self.printer_id = printer_id
        self.target_model = target_model
        self.target_printer_ids = target_printer_ids


class TestTargetOfIsTotal:
    """Every row shape answers exactly one kind, and the answer is never None."""

    def test_a_printer_id_alone_is_a_pin(self):
        target = target_of(_Row(printer_id=7))
        assert target.kind is TargetKind.PINNED
        assert target.printer_id == 7
        assert target.is_pool is False

    def test_a_target_model_alone_is_the_model_kind(self):
        target = target_of(_Row(target_model="H2S"))
        assert target.kind is TargetKind.MODEL
        assert target.model == "H2S"
        assert target.is_pool is True

    def test_a_target_printer_ids_alone_is_the_printers_kind(self):
        target = target_of(_Row(target_printer_ids="[1,3,5]"))
        assert target.kind is TargetKind.PRINTERS
        assert target.printer_ids == frozenset({1, 3, 5})
        assert target.is_pool is True

    def test_an_empty_row_is_unassigned(self):
        target = target_of(_Row())
        assert target.kind is TargetKind.UNASSIGNED
        assert target.is_pool is False

    def test_a_model_row_that_has_been_dispatched_is_still_the_pool_kind(self):
        """The dispatch RECORD is not a pin.

        A ``printing``/terminal pool row carries the printer it ran on beside its pool
        target (written at ``claim_pending_for_dispatch``). Reading that as PINNED
        would pin the unit's whole future to whichever machine served it once.
        """
        target = target_of(_Row(printer_id=4, target_model="H2S"))
        assert target.kind is TargetKind.MODEL
        assert target.model == "H2S"
        # The target does not carry the record — the row's own column does.
        assert target.printer_id is None

    def test_a_printers_row_that_has_been_dispatched_is_still_the_pool_kind(self):
        target = target_of(_Row(printer_id=3, target_printer_ids="[1,3,5]"))
        assert target.kind is TargetKind.PRINTERS
        assert target.printer_ids == frozenset({1, 3, 5})
        assert target.printer_id is None

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_target_model_is_not_the_model_kind(self, blank):
        """Whitespace is not a target: it would produce a MODEL kind matching nothing."""
        assert target_of(_Row(target_model=blank)).kind is TargetKind.UNASSIGNED
        assert target_of(_Row(printer_id=2, target_model=blank)).kind is TargetKind.PINNED

    def test_the_pool_column_outranks_the_model_column(self):
        """Precedence is stated, not accidental: PRINTERS wins over MODEL."""
        assert target_of(_Row(target_model="H2S", target_printer_ids="[2]")).kind is TargetKind.PRINTERS


class TestPrinterIdCodec:
    """Canonical form is a DOMAIN rule; the codec only serialises it."""

    def test_encoding_is_sorted_deduped_and_space_free(self):
        assert encode_printer_ids([3, 1, 5, 3, 1]) == "[1,3,5]"

    def test_it_round_trips(self):
        assert decode_printer_ids(encode_printer_ids([5, 1, 3])) == frozenset({1, 3, 5})

    def test_none_and_empty_encode_to_null_never_to_an_empty_array(self):
        """``"[]"`` must not exist: two spellings of "no pool" is the
        ``api_keys.printer_ids`` repair migration all over again."""
        assert encode_printer_ids(None) is None
        assert encode_printer_ids([]) is None
        assert encode_printer_ids(frozenset()) is None

    @pytest.mark.parametrize("stored", [None, "", "   ", "[]", " [] "])
    def test_the_empty_forms_all_decode_to_no_pool(self, stored):
        assert decode_printer_ids(stored) == frozenset()

    @pytest.mark.parametrize(
        "stored",
        [
            "not json at all",
            "[1,3",
            '{"1": 2}',
            "1,3,5",
            '["1","3"]',
            "[1, null, 3]",
            "[1, 2.5]",
            "[true, false]",
        ],
    )
    def test_malformed_values_decode_to_no_pool_without_raising(self, stored):
        """Whole-value rejection, and never an exception: this column has exactly one
        writer, so anything it would not have produced is not half-honoured."""
        assert decode_printer_ids(stored) == frozenset()

    def test_a_hand_written_unsorted_value_still_decodes(self):
        """Canonical form is how it is WRITTEN; reading stays tolerant of order."""
        assert decode_printer_ids("[5, 1, 3]") == frozenset({1, 3, 5})


class TestConstructorsValidateTheirOwnShape:
    def test_an_empty_printers_set_is_refused(self):
        with pytest.raises(ValueError):
            DispatchTarget.printers([])

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_model_is_refused(self, name):
        with pytest.raises(ValueError):
            DispatchTarget.for_model(name)

    def test_a_pin_without_a_printer_is_refused(self):
        with pytest.raises(ValueError):
            DispatchTarget.pinned(None)

    def test_printers_dedupes_into_a_frozenset(self):
        assert DispatchTarget.printers([3, 1, 3]).printer_ids == frozenset({1, 3})

    def test_unassigned_carries_nothing(self):
        target = DispatchTarget.unassigned()
        assert (target.kind, target.printer_id, target.model, target.printer_ids) == (
            TargetKind.UNASSIGNED,
            None,
            None,
            frozenset(),
        )


class TestMatches:
    def test_a_pool_member_matches_and_a_non_member_does_not(self):
        target = DispatchTarget.printers([1, 3, 5])
        assert target.matches(3, "H2S") is True
        assert target.matches(2, "H2S") is False

    def test_the_pool_ignores_the_model_entirely(self):
        """Membership is by id — a subset may mix models, and the caller's own
        capability gate is what refuses an unsuitable member."""
        assert DispatchTarget.printers([1]).matches(1, "H2C") is True

    def test_a_model_matches_the_same_spelling(self):
        assert DispatchTarget.for_model("H2S").matches(1, "H2S") is True

    def test_a_model_match_is_case_insensitive(self):
        assert DispatchTarget.for_model("H2S").matches(1, "h2s") is True

    def test_a_display_name_target_matches_the_stored_short_name(self):
        """The normalisation runs on the TARGET — the rule
        ``print_scheduler._find_idle_printer_for_target`` inherits through ``printer_filter``."""
        assert DispatchTarget.for_model("Bambu Lab H2S").matches(1, "H2S") is True

    def test_a_different_model_never_matches(self):
        assert DispatchTarget.for_model("H2S").matches(1, "H2C") is False

    @pytest.mark.parametrize("printer_model", [None, "", "   "])
    def test_a_printer_with_no_model_matches_nothing(self, printer_model):
        """An unknown model is not a wildcard."""
        assert DispatchTarget.for_model("H2S").matches(1, printer_model) is False

    def test_a_pin_matches_only_its_own_printer(self):
        target = DispatchTarget.pinned(7)
        assert target.matches(7, "H2S") is True
        assert target.matches(8, "H2S") is False

    @pytest.mark.parametrize("printer_id,model", [(1, "H2S"), (99, None), (0, "")])
    def test_unassigned_matches_nothing_at_all(self, printer_id, model):
        assert DispatchTarget.unassigned().matches(printer_id, model) is False


class TestPrinterFilterAgreesWithMatches:
    """One predicate, two dialects — they must select the same printers.

    The seeded fleet deliberately includes an INACTIVE H2S: ``printer_filter`` is the
    MEMBERSHIP half only, so it must return that row exactly as ``matches`` does. The
    liveness filters (``is_active``, ``quarantined``) belong to the scheduler and are
    ANDed on by the caller — a filter that quietly dropped inactive members would make
    the two predicates disagree and hide the caller's own omission.
    """

    @pytest.fixture
    async def fleet(self, db_session):
        printers = [
            Printer(name="001-H2S", serial_number="S001", ip_address="10.0.0.1", access_code="1", model="H2S"),
            Printer(name="003-H2S", serial_number="S003", ip_address="10.0.0.3", access_code="1", model="H2S"),
            Printer(name="008-H2C", serial_number="S008", ip_address="10.0.0.8", access_code="1", model="H2C"),
            Printer(
                name="012-H2S",
                serial_number="S012",
                ip_address="10.0.0.12",
                access_code="1",
                model="H2S",
                is_active=False,
            ),
        ]
        db_session.add_all(printers)
        await db_session.commit()
        result = await db_session.execute(select(Printer).order_by(Printer.id))
        return list(result.scalars().all())

    async def _assert_agreement(self, db_session, fleet, target: DispatchTarget) -> list[int]:
        expected = sorted(p.id for p in fleet if target.matches(p.id, p.model))
        rows = await db_session.execute(select(Printer.id).where(target.printer_filter()))
        assert sorted(row[0] for row in rows.all()) == expected
        return expected

    async def test_a_printers_target_selects_exactly_its_members(self, db_session, fleet):
        pool = [fleet[0].id, fleet[3].id]  # one active H2S + the INACTIVE H2S
        selected = await self._assert_agreement(db_session, fleet, DispatchTarget.printers(pool))
        assert selected == sorted(pool)

    async def test_a_model_target_selects_every_printer_of_that_model(self, db_session, fleet):
        selected = await self._assert_agreement(db_session, fleet, DispatchTarget.for_model("H2S"))
        assert selected == sorted([fleet[0].id, fleet[1].id, fleet[3].id])

    async def test_a_display_name_model_target_agrees_too(self, db_session, fleet):
        """The normalisation must happen on both sides of the predicate."""
        selected = await self._assert_agreement(db_session, fleet, DispatchTarget.for_model("Bambu Lab H2C"))
        assert selected == [fleet[2].id]

    async def test_a_pin_selects_one_printer(self, db_session, fleet):
        selected = await self._assert_agreement(db_session, fleet, DispatchTarget.pinned(fleet[1].id))
        assert selected == [fleet[1].id]

    async def test_unassigned_selects_nothing(self, db_session, fleet):
        assert await self._assert_agreement(db_session, fleet, DispatchTarget.unassigned()) == []

    async def test_a_pool_naming_a_deleted_printer_simply_narrows(self, db_session, fleet):
        """The accepted integrity cost of the denormalisation, asserted rather than
        assumed: a stale id is in neither answer."""
        target = DispatchTarget.printers([fleet[0].id, 9_999])
        selected = await self._assert_agreement(db_session, fleet, target)
        assert selected == [fleet[0].id]


class TestForRun:
    def test_a_printer_subset_becomes_the_pool_kind(self):
        target = DispatchTarget.for_run(printer_ids=[5, 1], target_model=None)
        assert target.kind is TargetKind.PRINTERS
        assert target.printer_ids == frozenset({1, 5})

    def test_a_subset_outranks_a_model(self):
        """``printer_ids`` XOR ``target_model`` at the schema; the service still states
        the precedence rather than trusting it."""
        assert DispatchTarget.for_run(printer_ids=[2], target_model="H2S").kind is TargetKind.PRINTERS

    def test_a_display_name_is_normalised(self):
        assert DispatchTarget.for_run(printer_ids=None, target_model="Bambu Lab H2S").model == "H2S"

    def test_a_short_name_passes_through(self):
        assert DispatchTarget.for_run(printer_ids=[], target_model="H2S").model == "H2S"

    def test_a_device_code_survives_the_run_normalisation_verbatim(self):
        """``O1S`` stays ``O1S`` — the run's normalisation, replicated warts and all.

        Pinned as a MIRROR, not an endorsement. ``production_run.py:204`` reads
        ``normalize_printer_model(raw) or normalize_printer_model_id(raw) or raw``, and
        ``normalize_printer_model`` answers for every non-blank input (an unknown name
        comes back with the ``"Bambu Lab "`` prefix stripped, i.e. ``"O1S"`` itself), so
        the ``_id`` fallback is unreachable and a device code is never mapped to its
        short name. Making this object smarter than the service would mean a run's
        stored ``target_model`` and this object's disagreed — the exact drift the
        replication exists to prevent. Capsule report: divergence D1.
        """
        assert DispatchTarget.for_run(printer_ids=None, target_model="O1S").model == "O1S"

    @pytest.mark.parametrize(
        "printer_ids,target_model",
        [(None, None), ([], None), (None, ""), ([], "   ")],
    )
    def test_no_target_at_all_is_a_value_error(self, printer_ids, target_model):
        """Never a silent UNASSIGNED: the schema already forbids this, so reaching it
        means the service lost its input, and a never-dispatchable unit would hide it."""
        with pytest.raises(ValueError):
            DispatchTarget.for_run(printer_ids=printer_ids, target_model=target_model)


class TestFromPlan:
    def test_a_plan_naming_printers_becomes_the_pool_kind(self):
        plan = {"remaining": 3, "printer_ids": [3, 1], "target_model": None}
        target = DispatchTarget.from_plan(plan)
        assert target.kind is TargetKind.PRINTERS
        assert target.printer_ids == frozenset({1, 3})

    def test_a_plan_naming_a_model_becomes_the_model_kind(self):
        target = DispatchTarget.from_plan({"printer_ids": None, "target_model": "H2S"})
        assert target.kind is TargetKind.MODEL
        assert target.model == "H2S"

    def test_a_stored_model_is_never_re_normalised(self):
        """``build_first_article_plan`` stores ``production_run``'s already-normalised
        value; running it through the normaliser again is how a second spelling gets
        minted."""
        assert DispatchTarget.from_plan({"printer_ids": None, "target_model": "H2S"}).model == "H2S"

    @pytest.mark.parametrize(
        "plan",
        [
            {},
            {"printer_ids": None, "target_model": None},
            {"printer_ids": [], "target_model": "  "},
            {"printer_ids": "not a list", "target_model": None},
        ],
    )
    def test_a_targetless_plan_is_a_value_error(self, plan):
        with pytest.raises(ValueError):
            DispatchTarget.from_plan(plan)


class TestFields:
    """All three keys, always — so a template can never carry a stale target column."""

    def test_a_pin_writes_only_the_printer_id(self):
        assert DispatchTarget.pinned(7).fields() == {
            "printer_id": 7,
            "target_model": None,
            "target_printer_ids": None,
        }

    def test_a_model_writes_only_the_model(self):
        assert DispatchTarget.for_model("H2S").fields() == {
            "printer_id": None,
            "target_model": "H2S",
            "target_printer_ids": None,
        }

    def test_a_pool_writes_only_the_canonical_id_list(self):
        assert DispatchTarget.printers([5, 1, 3]).fields() == {
            "printer_id": None,
            "target_model": None,
            "target_printer_ids": "[1,3,5]",
        }

    def test_unassigned_writes_three_nulls(self):
        assert DispatchTarget.unassigned().fields() == {
            "printer_id": None,
            "target_model": None,
            "target_printer_ids": None,
        }

    def test_the_fields_round_trip_back_through_target_of(self):
        for target in (
            DispatchTarget.pinned(7),
            DispatchTarget.for_model("H2S"),
            DispatchTarget.printers([5, 1]),
            DispatchTarget.unassigned(),
        ):
            assert target_of(_Row(**target.fields())) == target


_NAMES = {1: "001-H2S", 3: "003-H2S", 7: "007-H2C"}


class TestLabel:
    def test_a_model_labels_as_the_model(self):
        assert DispatchTarget.for_model("H2S").label(_NAMES) == "H2S"

    def test_a_pool_labels_its_members_in_id_order(self):
        assert DispatchTarget.printers([3, 1]).label(_NAMES) == "001-H2S, 003-H2S"

    def test_a_missing_name_falls_back_to_the_id(self):
        """A target that silently listed fewer printers than it holds would be worse
        than an ugly one."""
        assert DispatchTarget.printers([1, 4]).label(_NAMES) == "001-H2S, #4"

    def test_a_pin_labels_as_its_printer(self):
        assert DispatchTarget.pinned(7).label(_NAMES) == "007-H2C"

    def test_without_a_names_map_every_printer_reads_as_its_id(self):
        assert DispatchTarget.printers([3, 1]).label() == "#1, #3"
        assert DispatchTarget.pinned(7).label() == "#7"
        assert DispatchTarget.for_model("H2S").label() == "H2S"

    def test_unassigned_labels_as_nothing(self):
        assert DispatchTarget.unassigned().label(_NAMES) == ""
        assert DispatchTarget.unassigned().label() == ""


class TestDescribe:
    def test_a_model_reads_as_any_of_that_model(self):
        assert DispatchTarget.for_model("H2S").describe(_NAMES) == "Any H2S"

    def test_a_pool_reads_as_any_of_its_members(self):
        assert DispatchTarget.printers([3, 1]).describe(_NAMES) == "Any of 001-H2S, 003-H2S"

    def test_a_pin_reads_as_the_printer_alone(self):
        """No "Any": the whole point of a pin is that there is no choice."""
        assert DispatchTarget.pinned(7).describe(_NAMES) == "007-H2C"

    def test_without_a_names_map_the_phrase_still_names_ids(self):
        assert DispatchTarget.printers([1]).describe() == "Any of #1"
        assert DispatchTarget.pinned(7).describe() == "#7"

    def test_unassigned_says_so(self):
        assert DispatchTarget.unassigned().describe(_NAMES) == "Unassigned"

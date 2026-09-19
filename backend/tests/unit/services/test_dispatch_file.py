"""Tests for the dispatch-file seam — "which bytes does this dispatch upload".

The donors here are REAL-SHAPED: the plate member is a verbatim corpus machine-start head
(the same ``fixtures/chute_prime/*.head.gcode`` the rewrite's own goldens are cut from)
plus a short print tail, packed into a container that carries the members a sliced
``.gcode.3mf`` carries — an ``.md5`` sidecar, a config member, the model stub and a
STORE'd preview. A hand-written stub would let a container-level regression (a lost
sidecar, a re-deflated PNG, a member dropped by the repack) pass unnoticed, and those are
exactly the failures the printer reports as "the content of print file is unreadable".

The settings STORE is faked (one dict behind ``get_setting``): what is under test is the
seam's decisions — which steps are in the stack, what the key is made of, what is logged
and which bytes come back — not SQLAlchemy. Persistence and the PUT coercion whitelist
are pinned by ``integration/test_settings_api.py``.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.app.core.config import settings as app_settings
from backend.app.services import chute_prime, derived_3mf_cache, dispatch_file
from backend.app.services.bambu_ftp import cleanup_downloaded_3mf
from backend.app.services.dispatch_file import DispatchFile, build_dispatch_file

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "chute_prime"

LOGGER_NAME = "backend.app.services.dispatch_file"

#: A short print body under the machine-start head. ``; EXECUTABLE_BLOCK_END`` has to be
#: real: it is the END snippet's anchor, and a fixture without it would silently exercise
#: the append-to-EOF fallback instead.
_TAIL = "; LAYER_CHANGE\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F1800\nM400\n; EXECUTABLE_BLOCK_END\n"

_SNIPPETS = {"H2S": {"start_gcode": "M117 farm start", "end_gcode": "M117 farm end"}}

_MARKER = "FARM CHUTE PRIME"
_START_SNIPPET = "M117 farm start"


def head_of(stem: str = "h2s_canonical") -> bytes:
    return (FIXTURES / f"{stem}.head.gcode").read_bytes()


def plate_member(head: bytes | None = None) -> bytes:
    return (head if head is not None else head_of()) + _TAIL.encode("utf-8")


def make_3mf(tmp_path: Path, member: bytes, *, plate_id: int = 1, name: str = "donor.gcode.3mf") -> Path:
    """A container shaped like a sliced plate: gcode + sidecar + config + stub + preview."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", member)
        zf.writestr(f"Metadata/plate_{plate_id}.gcode.md5", "STALEHASHVALUE")
        zf.writestr("Metadata/slice_info.config", "<config></config>")
        zf.writestr("3D/3dmodel.model", "<model></model>")
        preview = zipfile.ZipInfo(f"Metadata/plate_{plate_id}.png")
        preview.compress_type = zipfile.ZIP_STORED
        zf.writestr(preview, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return path


def derived_member(path: Path, plate_id: int = 1) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(f"Metadata/plate_{plate_id}.gcode").decode("utf-8")


def warnings_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == LOGGER_NAME]


def errors_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR" and r.name == LOGGER_NAME]


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """The seam resolves its namespace off the DATA ROOT, so a test that did not redirect
    it would write real artifacts into the repo's data dir — and inherit the previous
    test's entries."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path / "data")
    derived_3mf_cache._build_locks.clear()
    derived_3mf_cache._swept_namespaces.clear()
    yield
    derived_3mf_cache._build_locks.clear()
    derived_3mf_cache._swept_namespaces.clear()


@pytest.fixture
def settings_store(monkeypatch) -> dict[str, str]:
    """The settings rows this dispatch reads. An absent key is an absent row, which is
    the schema default — the same thing a fresh install has."""
    values: dict[str, str] = {}

    async def _get_setting(db, key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", _get_setting)
    return values


async def dispatch(source: Path, *, plate_id: int = 1, item_id: int = 7, injection: bool = False, model="H2S"):
    return await build_dispatch_file(
        None, source, plate_id, item_id=item_id, gcode_injection=injection, printer_model=model
    )


class TestStackOrder:
    async def test_prime_runs_first_and_the_start_snippet_lands_after_the_rewritten_section(
        self, tmp_path, settings_store
    ):
        """Order is load-bearing, not cosmetic. The prime step swaps a byte PREFIX and
        asserts the member still starts with the head it read; run it second — after the
        snippet step has edited that same head — and it raises instead of applying. So a
        derived file carrying BOTH the rewritten section and the snippet is the proof the
        stack ran prime-first."""
        settings_store["gcode_snippets"] = json.dumps(_SNIPPETS)
        source = make_3mf(tmp_path, plate_member())

        result = await dispatch(source, injection=True)

        assert result.outcome == "derived"
        member = derived_member(result.path)
        assert _MARKER in member
        assert _START_SNIPPET in member
        # The snippet's anchor (; MACHINE_START_GCODE_END) sits BELOW the nozzle-load-line
        # section, so the rewritten section must precede it in the emitted member.
        assert member.index(_MARKER) < member.index(_START_SNIPPET)
        assert member.index(_START_SNIPPET) < member.index("; MACHINE_START_GCODE_END")
        assert member.rstrip().endswith("; EXECUTABLE_BLOCK_END")
        result.path.unlink(missing_ok=True)


class TestRollbackLever:
    async def test_flipping_the_setting_changes_the_key_and_the_bytes(self, tmp_path, settings_store):
        """The kill switch has to move the MACHINE, not just the code path. Because the
        key is derived from the stack, dropping the prime step drops its fingerprint —
        so the second dispatch cannot be served the primed artifact the first one cached.
        Snippets stay configured throughout, so both dispatches derive a file and the
        only variable is the switch."""
        settings_store["gcode_snippets"] = json.dumps(_SNIPPETS)
        source = make_3mf(tmp_path, plate_member())
        cache_dir = app_settings.base_dir / "dispatch_cache"

        primed = await dispatch(source, injection=True)
        assert primed.outcome == "derived"
        assert _MARKER in derived_member(primed.path)

        settings_store["farm_chute_prime_enabled"] = "false"
        rolled_back = await dispatch(source, injection=True)

        assert rolled_back.outcome == "derived"
        member = derived_member(rolled_back.path)
        assert _MARKER not in member, "a cached chute-primed artifact was served after the switch went off"
        assert _START_SNIPPET in member
        # Two stacks, two fingerprints, two cache entries — the old one is simply not
        # reachable by the new key.
        assert len(list(cache_dir.glob("*.3mf"))) == 2

        primed.path.unlink(missing_ok=True)
        rolled_back.path.unlink(missing_ok=True)


class TestDerivedContainer:
    async def test_single_repack_rewrites_the_member_and_refreshes_the_sidecar(self, tmp_path, settings_store):
        """The sidecar is Bambu's own format — uppercase hex, no trailing newline — and a
        stale one is rejected at load (HMS 0500-4003). Every other member must come
        through the repack untouched, CRC and all."""
        import hashlib

        source = make_3mf(tmp_path, plate_member())

        result = await dispatch(source)

        assert result.outcome == "derived"
        with zipfile.ZipFile(result.path, "r") as derived, zipfile.ZipFile(source, "r") as original:
            member = derived.read("Metadata/plate_1.gcode")
            sidecar = derived.read("Metadata/plate_1.gcode.md5")
            assert _MARKER in member.decode("utf-8")
            assert sidecar == hashlib.md5(member, usedforsecurity=False).hexdigest().upper().encode("ascii")
            assert sidecar == sidecar.upper() and len(sidecar) == 32 and not sidecar.endswith(b"\n")
            assert sidecar != b"STALEHASHVALUE"

            assert derived.namelist() == original.namelist()
            for name in original.namelist():
                if name.startswith("Metadata/plate_1.gcode"):
                    continue
                assert derived.getinfo(name).CRC == original.getinfo(name).CRC, name
                assert derived.getinfo(name).compress_type == original.getinfo(name).compress_type, name
        result.path.unlink(missing_ok=True)

    async def test_the_temp_is_reapable_by_the_lane_that_consumes_it(self, tmp_path, settings_store):
        """``cleanup_downloaded_3mf`` only deletes under the scratch roots, so an artifact
        handed out from anywhere else would leak one full container per dispatch."""
        source = make_3mf(tmp_path, plate_member())

        result = await dispatch(source)

        assert result.outcome == "derived"
        assert result.path.resolve().parent == Path(tempfile.gettempdir()).resolve()
        assert cleanup_downloaded_3mf(result.path) is True
        assert not result.path.exists()

    async def test_the_donor_is_never_modified(self, tmp_path, settings_store):
        source = make_3mf(tmp_path, plate_member())
        before = source.read_bytes()

        result = await dispatch(source)

        assert result.outcome == "derived"
        assert source.read_bytes() == before
        assert _MARKER not in derived_member(source)
        result.path.unlink(missing_ok=True)


class TestEmptyStack:
    async def test_no_steps_means_unmodified_and_no_member_read(self, tmp_path, settings_store, monkeypatch):
        """With the switch off and no snippets there is nothing to apply, and the
        hundreds-of-MB member must not be touched at all — an "empty" transform that
        still repacked would cost a full ZIP rewrite per dispatch for nothing."""
        settings_store["farm_chute_prime_enabled"] = "false"
        source = make_3mf(tmp_path, plate_member())
        repack = MagicMock(name="transform_plate_gcode")
        monkeypatch.setattr(dispatch_file, "transform_plate_gcode", repack)

        result = await dispatch(source, injection=True)

        assert result == DispatchFile(None, "unmodified")
        repack.assert_not_called()

    async def test_injection_off_leaves_the_snippets_out_of_the_stack(self, tmp_path, settings_store):
        """``gcode_injection`` is the per-JOB toggle: snippets configured globally must not
        reach a unit that did not ask for them."""
        settings_store["farm_chute_prime_enabled"] = "false"
        settings_store["gcode_snippets"] = json.dumps(_SNIPPETS)
        source = make_3mf(tmp_path, plate_member())

        result = await dispatch(source, injection=False)

        assert result == DispatchFile(None, "unmodified")


class TestRefusalsAreLoggedNotRaised:
    async def test_an_unrecognised_start_block_dispatches_unmodified_with_one_warning(
        self, tmp_path, settings_store, caplog
    ):
        """Operator ruling (2026-09-19): a file the recipe cannot read SHIPS AS SLICED.
        The warning is the whole operator-facing signal, so it names the reason — and
        there is exactly one of it per dispatch, or the production log stops being
        readable on a corpus the fleet cannot rewrite."""
        # Drop BOTH nozzle-load-line markers (and only those) — a head with no section at
        # all, which is what a non-H2 or hand-edited file looks like to the recipe.
        head = b"\n".join(
            line
            for line in head_of().split(b"\n")
            if not (line.lstrip().startswith(b";=====") and b"load line" in line)
        )
        source = make_3mf(tmp_path, plate_member(head))

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            result = await dispatch(source, item_id=4242)

        assert result == DispatchFile(None, "unmodified")
        assert len(warnings_of(caplog)) == 1
        message = warnings_of(caplog)[0]
        assert "section_missing" in message
        assert "4242" in message and "donor.gcode.3mf" in message

    async def test_an_unreadable_start_block_dispatches_unmodified_with_one_warning(
        self, tmp_path, settings_store, caplog
    ):
        """No machine-start marker at all (a non-Bambu or truncated file): the bounded
        reader answers None, which means the same thing and is logged the same way."""
        source = make_3mf(tmp_path, b"G28\nG1 X0 Y0\nM400\n")

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            result = await dispatch(source)

        assert result == DispatchFile(None, "unmodified")
        assert len(warnings_of(caplog)) == 1
        assert "no machine-start block" in warnings_of(caplog)[0]

    async def test_an_already_primed_file_is_quiet(self, tmp_path, settings_store, caplog):
        """Reachable in production: ``foreign_archive`` archives the printer-resident —
        already rewritten — copy of a screen-restarted job, which can come back as a
        donor. That is normal, so it must not page the operator with a warning."""
        outcome = chute_prime.rewrite_head(head_of())
        assert isinstance(outcome, chute_prime.Rewritten)
        source = make_3mf(tmp_path, plate_member(outcome.head))

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            result = await dispatch(source)

        assert result == DispatchFile(None, "unmodified")
        assert warnings_of(caplog) == []
        assert any("already chute-primed" in r.getMessage() for r in caplog.records if r.levelname == "DEBUG")


class TestBuildFailure:
    async def test_a_raising_transform_is_an_error_and_the_original_ships(
        self, tmp_path, settings_store, caplog, monkeypatch
    ):
        """A build failure is NOT a refusal: the recipe said yes and the machinery broke.
        It gets its own ERROR (a refusal is a WARNING), and the dispatch still goes out on
        the durable file rather than failing the print."""
        source = make_3mf(tmp_path, plate_member())

        def _boom(*args, **kwargs):
            raise RuntimeError("repack exploded")

        monkeypatch.setattr(dispatch_file, "transform_plate_gcode", _boom)
        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            result = await dispatch(source)

        assert result == DispatchFile(None, "build_failed")
        assert len(errors_of(caplog)) == 1
        assert "RuntimeError: repack exploded" in errors_of(caplog)[0]

    async def test_a_plate_the_container_lacks_is_a_build_failure(self, tmp_path, settings_store, caplog):
        """The builder answers None for an absent plate (never another plate's member),
        and the seam reports that as a build failure rather than uploading a file whose
        commanded plate is not in it."""
        settings_store["gcode_snippets"] = json.dumps(_SNIPPETS)
        source = make_3mf(tmp_path, plate_member(), plate_id=1)

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            result = await dispatch(source, plate_id=3, injection=True)

        assert result == DispatchFile(None, "build_failed")
        assert len(errors_of(caplog)) == 1
        assert "builder produced no file" in errors_of(caplog)[0]


class TestCaching:
    async def test_a_second_identical_dispatch_is_a_cache_hit(self, tmp_path, settings_store, monkeypatch):
        """Every unit of a run rebuilds byte-identical bytes out of the same donor. The
        second one must cost a copy, not a full ZIP read + re-deflate."""
        source = make_3mf(tmp_path, plate_member())
        builds: list[int] = []
        real = dispatch_file.transform_plate_gcode

        def _counting(source_path, plate_id, transform):
            builds.append(plate_id)
            return real(source_path, plate_id, transform)

        monkeypatch.setattr(dispatch_file, "transform_plate_gcode", _counting)

        first = await dispatch(source)
        second = await dispatch(source)

        assert first.outcome == "derived" and second.outcome == "derived"
        assert builds == [1], "the second dispatch rebuilt instead of hitting the cache"
        assert first.path != second.path
        assert derived_member(second.path) == derived_member(first.path)
        first.path.unlink(missing_ok=True)
        second.path.unlink(missing_ok=True)

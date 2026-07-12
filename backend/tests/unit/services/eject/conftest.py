"""Shared fixtures for the eject unit tests."""

import pytest

from backend.app.models.printer_model_geometry import PrinterModelGeometry


@pytest.fixture
async def seed_geometry(db_session):
    """Seed the H2S (validated) + H2C (unvalidated) geometry rows.

    The test DB is built with ``create_all`` only (no ``run_migrations``), so the
    registry seeds are absent — any code path that resolves geometry from the DB
    (dispatch, capability gate, the model-geometry routes) needs these rows.
    """
    rows = [
        PrinterModelGeometry(
            model_key="H2S",
            bed_x=340,
            bed_y=320,
            env_x_min=0,
            env_x_max=340,
            env_y_min=-16,
            env_y_max=325,
            max_part_height_mm=42,
            validated=True,
            notes="test seed",
        ),
        PrinterModelGeometry(
            model_key="H2C",
            bed_x=330,
            bed_y=320,
            env_x_min=15,
            env_x_max=325,
            env_y_min=0,
            env_y_max=320,
            max_part_height_mm=42,
            validated=False,
            notes="test seed",
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return rows

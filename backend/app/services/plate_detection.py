"""Build plate empty detection using OpenCV.

Analyzes camera frames to detect if there are objects on the build plate.
Uses calibration-based difference detection - compares current frame to
a reference image of the empty plate.

**A reading answers THREE ways** (:class:`PlateVerdict`, 2026-10-16): ``empty``, ``occupied``, or
``unknown`` with its reason — uncalibrated, OpenCV missing, the capture failed, the frame would not
decode, no frame taken after the reading's anchor (``not_before``), an analysis error. A failure used
to read as "empty". Each caller decides what ``unknown`` is worth: the print-start check never blocks
a print on it, and the auto plate-clear (``plate_autoclear``) counts it as occupied.

**The frame comes from the still owner** (``services/camera_stills``) under its READING policy: the
one capture implementation, which honours a live stream (#1348), never falls back from the external
camera to the built-in one, and serves an anchored read only a FRESH frame taken after its anchor. A reading the farm takes itself turns the chamber light on first and restores it
after (:func:`chamber_light_for_read`); the calibration and the auto plate-clear move the bed to the
held-bed position first (``eject.bed_motion.lift_held_bed``), so references and readings share one
bed height.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from backend.app.services import camera_stills, printer_action
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)

# The compared area when a printer has none of its own, as (x, y, width, height) shares of the frame:
# the WHOLE frame, so no part of the plate falls outside the comparison at any bed height. The
# operator narrows it per printer in the area editor (``printers.plate_detection_roi_*``) once the
# per-model witness shows where the plate sits at the held-bed position.
DEFAULT_ROI: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

# The chamber light's settle before a reading (seconds): the light takes ~1 s to come on and the
# camera adjusts its exposure after it — the print-start check's figure since upstream.
LIGHT_SETTLE_S = 2.5

# Optional OpenCV import - feature disabled if not available
try:
    import cv2
    import numpy as np

    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    logger.info("OpenCV not available - plate detection feature disabled")


def _get_calibration_dir() -> Path:
    """Get the calibration directory from settings (ensures persistence in Docker)."""
    from backend.app.core.config import settings

    return settings.plate_calibration_dir


PlateVerdictKind = Literal["empty", "occupied", "unknown"]
# Why a reading is ``unknown``.
UnknownReason = Literal[
    "opencv_unavailable", "uncalibrated", "capture_failed", "decode_failed", "stale_frame", "analysis_error"
]


@dataclass(frozen=True)
class PlateVerdict:
    """One plate reading: ``empty`` / ``occupied`` / ``unknown`` (with ``reason``).

    ``captured_at`` is the frame's own instant (None when no frame was read) — what the auto
    plate-clear's two-reading rule compares, so one frame never counts twice.
    """

    kind: PlateVerdictKind
    message: str
    reason: UnknownReason | None = None
    confidence: float = 0.0  # 0.0 to 1.0
    difference_percent: float = 0.0  # how different from the best reference
    captured_at: datetime | None = None
    debug_image: bytes | None = None

    @property
    def is_empty(self) -> bool:
        return self.kind == "empty"

    @property
    def needs_calibration(self) -> bool:
        return self.reason == "uncalibrated"

    def to_dict(self) -> dict:
        return {
            "verdict": self.kind,
            "reason": self.reason,
            "is_empty": self.is_empty,
            "confidence": float(round(self.confidence, 2)),
            "difference_percent": float(round(self.difference_percent, 2)),
            "message": self.message,
            "has_debug_image": self.debug_image is not None,
            "needs_calibration": self.needs_calibration,
        }


def _unknown(reason: UnknownReason, message: str) -> PlateVerdict:
    return PlateVerdict(kind="unknown", reason=reason, message=message)


def roi_of(printer: Printer) -> tuple[float, float, float, float] | None:
    """The printer's own compared area, or None (then :data:`DEFAULT_ROI`). The one reader of the
    ``plate_detection_roi_*`` columns for a reading."""
    values = (
        printer.plate_detection_roi_x,
        printer.plate_detection_roi_y,
        printer.plate_detection_roi_w,
        printer.plate_detection_roi_h,
    )
    if any(value is None for value in values):
        return None
    x, y, w, h = values
    return (float(x), float(y), float(w), float(h))  # type: ignore[arg-type]


class PlateDetector:
    """Detects if the build plate is empty using calibration-based difference detection."""

    # Detection thresholds for difference detection
    # Using mean pixel difference (0-100% scale)
    # Small objects may only cause 1-2% mean difference
    DEFAULT_DIFFERENCE_THRESHOLD = 1.0
    DEFAULT_BLUR_SIZE = 21  # Gaussian blur kernel size (must be odd) - unused with edge detection

    def __init__(
        self,
        roi: tuple[float, float, float, float] | None = None,
        difference_threshold: float = DEFAULT_DIFFERENCE_THRESHOLD,
        blur_size: int = DEFAULT_BLUR_SIZE,
    ):
        """Initialize the plate detector.

        Args:
            roi: Region of interest as (x%, y%, w%, h%) - percentages of image size
            difference_threshold: Percentage of pixels that must differ to trigger "not empty"
            blur_size: Gaussian blur kernel size for noise reduction
        """
        if not OPENCV_AVAILABLE:
            raise RuntimeError("OpenCV is not installed. Install with: pip install opencv-python-headless")

        self.roi = roi or DEFAULT_ROI
        self.difference_threshold = difference_threshold
        self.blur_size = blur_size if blur_size % 2 == 1 else blur_size + 1  # Must be odd

    # Maximum number of reference images to store per printer
    MAX_REFERENCES = 5

    def _get_metadata_path(self, printer_id: int) -> Path:
        """Get the path to the metadata JSON file for a printer."""
        _get_calibration_dir().mkdir(parents=True, exist_ok=True)
        return _get_calibration_dir() / f"printer_{printer_id}_metadata.json"

    def _load_metadata(self, printer_id: int) -> dict:
        """Load metadata for a printer's references."""
        import json

        meta_path = self._get_metadata_path(printer_id)
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                pass
        return {"references": {}}

    def _save_metadata(self, printer_id: int, metadata: dict) -> None:
        """Save metadata for a printer's references."""
        import json

        meta_path = self._get_metadata_path(printer_id)
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def _get_reference_paths(self, printer_id: int) -> list[Path]:
        """Get all existing reference image paths for a printer."""
        _get_calibration_dir().mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(self.MAX_REFERENCES):
            path = _get_calibration_dir() / f"printer_{printer_id}_ref_{i}.jpg"
            if path.exists():
                paths.append(path)
        return paths

    def _get_next_reference_slot(self, printer_id: int) -> Path:
        """Get the path for the next reference image slot (cycles through slots)."""
        _get_calibration_dir().mkdir(parents=True, exist_ok=True)
        # Find first empty slot, or use oldest (slot 0) and shift others
        for i in range(self.MAX_REFERENCES):
            path = _get_calibration_dir() / f"printer_{printer_id}_ref_{i}.jpg"
            if not path.exists():
                return path
        # All slots full - return slot 0 (will be overwritten, but we rotate first)
        return _get_calibration_dir() / f"printer_{printer_id}_ref_0.jpg"

    def _rotate_references(self, printer_id: int) -> None:
        """Rotate references: delete oldest (0), shift others down."""
        # Delete slot 0
        slot0 = _get_calibration_dir() / f"printer_{printer_id}_ref_0.jpg"
        if slot0.exists():
            logger.info("Rotating references: removing oldest %s", slot0)
            slot0.unlink()
        # Shift others down
        for i in range(1, self.MAX_REFERENCES):
            old_path = _get_calibration_dir() / f"printer_{printer_id}_ref_{i}.jpg"
            new_path = _get_calibration_dir() / f"printer_{printer_id}_ref_{i - 1}.jpg"
            if old_path.exists():
                old_path.rename(new_path)

        # Also rotate metadata
        metadata = self._load_metadata(printer_id)
        refs = metadata.get("references", {})
        new_refs = {}
        for i in range(1, self.MAX_REFERENCES):
            if str(i) in refs:
                new_refs[str(i - 1)] = refs[str(i)]
        metadata["references"] = new_refs
        self._save_metadata(printer_id, metadata)

    def get_references(self, printer_id: int) -> list[dict]:
        """Get all references with metadata for a printer.

        Returns list of dicts with: index, label, timestamp, has_image
        """

        metadata = self._load_metadata(printer_id)
        refs = metadata.get("references", {})
        result = []

        for i in range(self.MAX_REFERENCES):
            path = _get_calibration_dir() / f"printer_{printer_id}_ref_{i}.jpg"
            if path.exists():
                ref_meta = refs.get(str(i), {})
                result.append(
                    {
                        "index": i,
                        "label": ref_meta.get("label", ""),
                        "timestamp": ref_meta.get("timestamp", ""),
                        "has_image": True,
                    }
                )

        return result

    def update_reference_label(self, printer_id: int, index: int, label: str) -> bool:
        """Update the label for a reference."""
        if index < 0 or index >= self.MAX_REFERENCES:
            return False

        path = _get_calibration_dir() / f"printer_{printer_id}_ref_{index}.jpg"
        if not path.exists():
            return False

        metadata = self._load_metadata(printer_id)
        if "references" not in metadata:
            metadata["references"] = {}
        if str(index) not in metadata["references"]:
            metadata["references"][str(index)] = {}

        metadata["references"][str(index)]["label"] = label
        self._save_metadata(printer_id, metadata)
        return True

    def delete_reference(self, printer_id: int, index: int) -> bool:
        """Delete a specific reference by index."""
        if index < 0 or index >= self.MAX_REFERENCES:
            return False

        path = _get_calibration_dir() / f"printer_{printer_id}_ref_{index}.jpg"
        if not path.exists():
            return False

        # Delete image
        logger.info("Deleting reference %s for printer %s: %s", index, printer_id, path)
        path.unlink()

        # Remove from metadata
        metadata = self._load_metadata(printer_id)
        refs = metadata.get("references", {})
        if str(index) in refs:
            del refs[str(index)]
        metadata["references"] = refs
        self._save_metadata(printer_id, metadata)

        # Shift remaining references down to fill the gap
        for i in range(index + 1, self.MAX_REFERENCES):
            old_img = _get_calibration_dir() / f"printer_{printer_id}_ref_{i}.jpg"
            new_img = _get_calibration_dir() / f"printer_{printer_id}_ref_{i - 1}.jpg"
            if old_img.exists():
                old_img.rename(new_img)
                # Also shift metadata
                if str(i) in refs:
                    refs[str(i - 1)] = refs[str(i)]
                    del refs[str(i)]

        metadata["references"] = refs
        self._save_metadata(printer_id, metadata)
        return True

    def get_reference_thumbnail(self, printer_id: int, index: int, max_size: int = 150) -> bytes | None:
        """Get a thumbnail of a reference image.

        Returns JPEG bytes or None if not found.
        """
        path = _get_calibration_dir() / f"printer_{printer_id}_ref_{index}.jpg"
        if not path.exists():
            return None

        try:
            img = cv2.imread(str(path))
            if img is None:
                return None

            # Calculate thumbnail size maintaining aspect ratio
            h, w = img.shape[:2]
            if w > h:
                new_w = max_size
                new_h = int(h * max_size / w)
            else:
                new_h = max_size
                new_w = int(w * max_size / h)

            thumb = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            _, buffer = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buffer.tobytes()
        except Exception as e:
            logger.error("Error creating thumbnail: %s", e)
            return None

    def _extract_roi(self, frame: np.ndarray) -> tuple[np.ndarray, int, int, int, int]:
        """Extract the region of interest from a frame.

        Returns:
            Tuple of (roi_frame, x_start, y_start, roi_width, roi_height)
        """
        height, width = frame.shape[:2]
        x_start = int(width * self.roi[0])
        y_start = int(height * self.roi[1])
        roi_width = int(width * self.roi[2])
        roi_height = int(height * self.roi[3])
        roi_frame = frame[y_start : y_start + roi_height, x_start : x_start + roi_width]
        return roi_frame, x_start, y_start, roi_width, roi_height

    def _preprocess_for_comparison(self, frame: np.ndarray) -> np.ndarray:
        """Preprocess a frame for comparison.

        Uses heavy blur to create "blob" representation - smooths out texture
        and noise while preserving large objects. Then normalizes brightness
        to reduce lighting sensitivity.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Very heavy blur to smooth texture, keep only large shapes
        blurred = cv2.GaussianBlur(gray, (51, 51), 0)
        # Normalize to 0-255 range to reduce brightness sensitivity
        normalized = cv2.normalize(blurred, None, 0, 255, cv2.NORM_MINMAX)
        return normalized

    def calibrate(self, image_data: bytes, printer_id: int, label: str | None = None) -> tuple[bool, str, int]:
        """Calibrate by saving a reference image of the empty plate.

        Stores up to MAX_REFERENCES (5) images per printer. When all slots are full,
        the oldest reference is removed and others are shifted.

        Args:
            image_data: JPEG image data as bytes
            printer_id: Printer database ID
            label: Optional label for this reference (e.g., "High Temp Plate")

        Returns:
            Tuple of (success, message, index) where index is the slot used
        """
        from datetime import datetime

        try:
            # Decode image
            nparr = np.frombuffer(image_data, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if frame is None:
                return False, "Failed to decode image", -1

            # Get existing references count
            existing_refs = self._get_reference_paths(printer_id)
            num_existing = len(existing_refs)

            # If all slots are full, rotate (remove oldest)
            if num_existing >= self.MAX_REFERENCES:
                self._rotate_references(printer_id)
                num_existing = self.MAX_REFERENCES - 1

            # Save to next available slot
            slot_index = num_existing
            reference_path = _get_calibration_dir() / f"printer_{printer_id}_ref_{slot_index}.jpg"
            write_success = cv2.imwrite(str(reference_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

            if not write_success:
                logger.error("cv2.imwrite failed for %s", reference_path)
                return False, "Failed to save reference image", -1

            # Verify the file actually exists and has content
            if not reference_path.exists():
                logger.error("Reference image not found after save: %s", reference_path)
                return False, "Reference image not found after save", -1

            file_size = reference_path.stat().st_size
            if file_size < 1000:  # JPEG should be at least 1KB
                logger.error("Reference image too small (%s bytes): %s", file_size, reference_path)
                reference_path.unlink()  # Clean up invalid file
                return False, f"Reference image corrupted (only {file_size} bytes)", -1

            logger.info("Saved reference image: %s (%s bytes)", reference_path, file_size)

            # Save metadata
            metadata = self._load_metadata(printer_id)
            if "references" not in metadata:
                metadata["references"] = {}
            metadata["references"][str(slot_index)] = {
                "label": label or "",
                "timestamp": datetime.now().isoformat(),
            }
            self._save_metadata(printer_id, metadata)

            logger.info(
                f"Saved plate calibration reference {slot_index + 1}/{self.MAX_REFERENCES} for printer {printer_id}"
            )
            return True, f"Calibration saved ({slot_index + 1}/{self.MAX_REFERENCES} references)", slot_index

        except Exception as e:
            logger.exception("Error during plate calibration")
            # Don't expose exception details to user - log has full info
            error_type = type(e).__name__
            return False, f"Calibration error: {error_type}", -1

    def get_calibration_count(self, printer_id: int) -> int:
        """Get the number of calibration references for a printer."""
        return len(self._get_reference_paths(printer_id))

    def has_calibration(self, printer_id: int, plate_type: str | None = None) -> bool:
        """Check if a printer has any calibration reference images."""
        return len(self._get_reference_paths(printer_id)) > 0

    def delete_calibration(self, printer_id: int, plate_type: str | None = None) -> bool:
        """Delete all calibration reference images for a printer."""
        paths = self._get_reference_paths(printer_id)
        if not paths:
            return False
        for path in paths:
            path.unlink()
        logger.info("Deleted %s plate calibration reference(s) for printer %s", len(paths), printer_id)
        return True

    def analyze_frame(self, image_data: bytes, printer_id: int, include_debug_image: bool = False) -> PlateVerdict:
        """Analyze a camera frame to detect if the plate is empty.

        Compares the current frame to all calibration reference images and uses
        the best match (lowest difference) for the final result. Anything that
        prevents a comparison is ``unknown`` with its reason — never "empty".

        Args:
            image_data: JPEG image data as bytes
            printer_id: Printer database ID (for reference lookup)
            include_debug_image: If True, include annotated image in result
        """
        try:
            # Check for calibration
            reference_paths = self._get_reference_paths(printer_id)
            if not reference_paths:
                return _unknown("uncalibrated", "No calibration - please calibrate with empty plate first")

            # Decode current image
            nparr = np.frombuffer(image_data, np.uint8)
            current_frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if current_frame is None:
                return _unknown("decode_failed", "Failed to decode current image")

            # Extract ROI from current frame
            current_roi, x_start, y_start, roi_width, roi_height = self._extract_roi(current_frame)
            current_processed = self._preprocess_for_comparison(current_roi)

            # Compare against all references, find best match (lowest difference)
            best_difference_percent = float("inf")
            best_ref_idx = -1
            best_diff = None

            for idx, ref_path in enumerate(reference_paths):
                # Load reference image
                reference_frame = cv2.imread(str(ref_path), cv2.IMREAD_COLOR)
                if reference_frame is None:
                    continue

                # Ensure same dimensions
                if current_frame.shape != reference_frame.shape:
                    reference_frame = cv2.resize(reference_frame, (current_frame.shape[1], current_frame.shape[0]))

                # Extract ROI and preprocess
                reference_roi, _, _, _, _ = self._extract_roi(reference_frame)
                reference_processed = self._preprocess_for_comparison(reference_roi)

                # Calculate absolute difference
                diff = cv2.absdiff(current_processed, reference_processed)

                # Calculate mean difference as percentage
                mean_diff = np.mean(diff)
                difference_percent = (mean_diff / 255.0) * 100

                if difference_percent < best_difference_percent:
                    best_difference_percent = difference_percent
                    best_ref_idx = idx
                    best_diff = diff

            if best_ref_idx == -1:
                return _unknown("uncalibrated", "Failed to load any reference images - please recalibrate")

            difference_percent = best_difference_percent

            # Determine if plate is empty (use best match)
            is_empty = difference_percent < self.difference_threshold

            # Calculate confidence
            if is_empty:
                # Higher confidence when very little difference
                confidence = 1.0 - min(1.0, difference_percent / self.difference_threshold)
            else:
                # Higher confidence when clearly different
                confidence = min(1.0, difference_percent / (self.difference_threshold * 2))

            # Generate message
            num_refs = len(reference_paths)
            if is_empty:
                message = (
                    f"Plate appears empty (difference: {difference_percent:.1f}%, ref {best_ref_idx + 1}/{num_refs})"
                )
            else:
                message = f"Objects detected on plate (difference: {difference_percent:.1f}%, best ref {best_ref_idx + 1}/{num_refs})"

            # Generate debug image if requested
            debug_image = None
            if include_debug_image and best_diff is not None:
                debug_frame = current_frame.copy()

                # Draw ROI rectangle
                cv2.rectangle(
                    debug_frame,
                    (x_start, y_start),
                    (x_start + roi_width, y_start + roi_height),
                    (0, 255, 0),
                    2,
                )

                # Create colored difference overlay
                # Red = areas that are different from reference
                # Amplify diff for visibility (multiply by 3, cap at 255)
                diff_amplified = np.minimum(best_diff * 3, 255).astype(np.uint8)
                diff_colored = cv2.cvtColor(diff_amplified, cv2.COLOR_GRAY2BGR)
                diff_colored[:, :, 0] = 0  # Remove blue
                diff_colored[:, :, 1] = 0  # Remove green
                # Red channel has the diff

                # Overlay difference on ROI
                roi_overlay = debug_frame[y_start : y_start + roi_height, x_start : x_start + roi_width]
                cv2.addWeighted(diff_colored, 0.5, roi_overlay, 0.5, 0, roi_overlay)

                # Add status text
                status_text = "EMPTY" if is_empty else "OBJECTS DETECTED"
                color = (0, 255, 0) if is_empty else (0, 0, 255)
                cv2.putText(debug_frame, status_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)
                cv2.putText(
                    debug_frame,
                    f"Diff: {difference_percent:.1f}% (ref {best_ref_idx + 1}/{num_refs})",
                    (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    color,
                    2,
                )
                cv2.putText(
                    debug_frame,
                    f"Confidence: {confidence:.0%}",
                    (10, 90),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    color,
                    2,
                )

                # Encode debug image as JPEG
                _, buffer = cv2.imencode(".jpg", debug_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                debug_image = buffer.tobytes()

            return PlateVerdict(
                kind="empty" if is_empty else "occupied",
                confidence=float(confidence),
                difference_percent=float(difference_percent),
                message=message,
                debug_image=debug_image,
            )

        except Exception as e:
            logger.exception("Error analyzing frame for plate detection")
            return _unknown("analysis_error", f"Analysis error: {e!s}")


@asynccontextmanager
async def chamber_light_for_read(printer_id: int) -> AsyncIterator[datetime]:
    """The chamber light ON for a plate reading the farm takes itself, restored to OFF after it when this
    turned it on (the print-start check's precedent). Yields the reading's ANCHOR: the instant the light
    has settled (:data:`LIGHT_SETTLE_S`) — a frame taken before it is not this reading's. With no client
    (or a light already on) nothing is sent and the anchor is now. Both frames go through the command
    record (``printer_action.chamber_light``)."""
    client = printer_manager.get_client(printer_id)
    turned_on = False
    if client is not None and client.state is not None and not client.state.chamber_light:
        sent = await printer_action.send(
            printer_id, printer_action.chamber_light(True), actor=printer_action.ACTOR_FARM, client=client
        )
        if sent.published:
            turned_on = True
            await asyncio.sleep(LIGHT_SETTLE_S)
    try:
        yield datetime.now(timezone.utc)
    finally:
        if turned_on:
            await printer_action.send(
                printer_id, printer_action.chamber_light(False), actor=printer_action.ACTOR_FARM, client=client
            )


# Why no frame came back, as a reading's reason (``camera_stills.StillMiss`` → :data:`UnknownReason`).
_MISS_REASON: dict[str, UnknownReason] = {
    "capture_failed": "capture_failed",
    "no_printer": "capture_failed",
    "stream_stale": "stale_frame",
}


async def check_plate_empty(
    printer: Printer,
    *,
    use_external: bool | None = None,
    not_before: datetime | None = None,
    include_debug_image: bool = False,
) -> PlateVerdict:
    """One reading of ``printer``'s plate (:class:`PlateVerdict`).

    The frame is the still owner's (``camera_stills.latest_still``): with ``not_before`` (the reading's
    anchor — the bed settled, the light on, a person's click) only a frame taken after it; without, the
    owner's cached still. The compared area is the printer's own (:func:`roi_of`) or the whole frame.
    ``use_external`` None takes the printer's own camera setting (#1359).
    """
    if not OPENCV_AVAILABLE:
        return _unknown("opencv_unavailable", "OpenCV not available - plate detection disabled")
    detector = PlateDetector(roi=roi_of(printer))
    if not detector.has_calibration(printer.id):
        return _unknown("uncalibrated", "No calibration - please calibrate with empty plate first")
    still = await camera_stills.latest_still(
        printer.id,
        policy=camera_stills.READING,
        not_before=not_before,
        source=camera_stills.CameraSource.of(printer, use_external=use_external),
    )
    if isinstance(still, camera_stills.NoStill):
        reason = _MISS_REASON[still.reason]
        message = (
            "No camera frame taken after the reading started"
            if reason == "stale_frame"
            else "Failed to capture camera frame from any source"
        )
        return _unknown(reason, message)
    verdict = detector.analyze_frame(still.jpeg, printer.id, include_debug_image)
    return replace(verdict, captured_at=still.captured_at, message=f"[{still.source}] {verdict.message}")


# What the calibration says when the bed could not be moved to the reading height first
# (``eject.bed_motion`` refusals that are not permanent for the printer).
_CALIBRATION_MOTION_REFUSED: dict[str, str] = {
    "job_active": "Calibration refused: a job is running on the printer.",
    "dispatch_in_flight": "Calibration refused: a print is being dispatched to the printer.",
    "eject_in_flight": "Calibration refused: an eject is running on the printer.",
    "bed_motion_in_flight": "Calibration refused: the bed is moving.",
    "no_client": "Calibration refused: printer not connected, bed not positioned.",
    "not_sent": "Calibration refused: the printer did not take the bed move.",
}


async def calibrate_plate(
    db: AsyncSession,
    printer: Printer,
    *,
    label: str | None = None,
    use_external: bool | None = None,
) -> tuple[bool, str, int]:
    """Save a reference image of ``printer``'s EMPTY plate. Returns ``(success, message, index)``.

    The bed first goes to the held-bed position (``eject.bed_motion.lift_held_bed`` — guarded onto the
    bottom stop, then lifted off the eject aids; a CLAIMED motion), the position the auto plate-clear
    reads from, and the reference is a frame taken after the motion settled. A printer with no bed on
    Z (a bedslinger, a model with no Z travel) is calibrated where its bed is; a motion refused for any
    other reason refuses the calibration rather than save a reference at another height.
    """
    if not OPENCV_AVAILABLE:
        return False, "OpenCV not available - plate detection disabled", -1

    from backend.app.services.eject import bed_motion

    motion = await bed_motion.lift_held_bed(db, printer.id)
    if isinstance(motion, bed_motion.MotionSkipped) and not motion.permanent:
        logger.info("[plate-detection] printer %s calibration refused: bed motion %s", printer.id, motion.reason)
        return False, _CALIBRATION_MOTION_REFUSED.get(motion.reason, "Calibration refused: bed not positioned."), -1
    if isinstance(motion, bed_motion.MotionSent):
        # No end-of-motion report exists on the wire: the motion has settled when its claimed window lapses.
        await asyncio.sleep(motion.seconds)

    still = await camera_stills.latest_still(
        printer.id,
        policy=camera_stills.READING,
        not_before=datetime.now(timezone.utc),
        source=camera_stills.CameraSource.of(printer, use_external=use_external),
    )
    if isinstance(still, camera_stills.NoStill):
        return False, "Failed to capture camera frame for calibration", -1

    detector = PlateDetector()
    success, message, index = detector.calibrate(still.jpeg, printer.id, label)

    if success:
        message = f"[{still.source}] {message}"

    return success, message, index


def get_calibration_status(printer_id: int, plate_type: str | None = None) -> dict:
    """Get calibration status for a printer.

    Returns:
        Dict with calibration info including reference count
    """
    if not OPENCV_AVAILABLE:
        return {
            "available": False,
            "calibrated": False,
            "reference_count": 0,
            "max_references": 5,
            "message": "OpenCV not available",
        }

    detector = PlateDetector()
    calibrated = detector.has_calibration(printer_id)
    ref_count = detector.get_calibration_count(printer_id)

    if calibrated:
        message = f"Calibrated with {ref_count}/{detector.MAX_REFERENCES} reference(s)"
    else:
        message = "Not calibrated - please calibrate with empty plate"

    return {
        "available": True,
        "calibrated": calibrated,
        "reference_count": ref_count,
        "max_references": detector.MAX_REFERENCES,
        "message": message,
    }


def delete_calibration(printer_id: int, plate_type: str | None = None) -> bool:
    """Delete calibration for a printer and plate type."""
    if not OPENCV_AVAILABLE:
        return False

    detector = PlateDetector()
    return detector.delete_calibration(printer_id, plate_type)


def is_plate_detection_available() -> bool:
    """Check if plate detection feature is available (OpenCV installed)."""
    return OPENCV_AVAILABLE

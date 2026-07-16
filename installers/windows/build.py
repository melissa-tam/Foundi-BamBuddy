"""Build script for the Bambuddy Windows installer.

Stages all artifacts under ``installers/windows/build/staging/`` for the
Inno Setup compiler to package. Run this on Windows (or in a Windows CI
runner) — it pip-installs Bambuddy's deps against the embedded Python it
downloads, which requires the matching platform.

Steps:
    1. Download python.org embeddable distribution for Windows x64
    2. Configure embedded Python (allow site-packages)
    3. Bootstrap pip into the embedded distribution
    4. Install ``requirements.txt`` into the embedded Python
    5. Build the React frontend (``frontend/npm run build``)
    6. Stage backend source + frontend bundle
    7. Download NSSM
    8. Download ffmpeg static build for Windows
    9. Print "ready for ISCC" message

After this script succeeds, run::

    "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" bambuddy.iss

to produce the final installer .exe under ``build/output/``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Repo root: installers/windows/build.py -> ../../
REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = Path(__file__).resolve().parent
BUILD_DIR = INSTALLER_DIR / "build"
STAGING = BUILD_DIR / "staging"
DOWNLOADS = BUILD_DIR / "downloads"

# Python 3.13 — matches Dockerfile (python:3.13-slim-trixie). Bump when
# the Dockerfile bumps; the Windows installer should track production.
PYTHON_VERSION = "3.13.1"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"

# NSSM 2.24 is the long-time stable build (no new release since 2014).
# Vendored under installers/windows/vendor/nssm.exe rather than fetched
# at build time — nssm.cc has flaked with 503s mid-CI-run before, and
# pinning to a checked-in binary makes builds reproducible and lets us
# inspect the binary in PRs if it ever needs updating. SHA-256:
#   f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97
NSSM_VERSION = "2.24"

# ffmpeg static build. BtbN's gyan-equivalent build is the most reliable
# automated source. Pin to a release tag so builds are reproducible.
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

# get-pip.py for bootstrapping pip into the embedded distribution
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def _embedded_env() -> dict:
    """Environment for subprocesses that run the *embedded* python.exe.

    The embedded distribution's ``._pth`` enables ``import site`` so that
    pip-installed packages under ``Lib\\site-packages`` are importable. But
    enabling site also pulls in the build machine's per-user site-packages
    (``%APPDATA%\\Python\\Python3XX\\site-packages``) onto ``sys.path``. When
    the build box already has Bambuddy's deps installed there (a dev's
    ``py -3.13`` environment), pip reports them "already satisfied" and
    SKIPS installing them into the embedded tree — so numpy, matplotlib,
    requests, certifi, setuptools, etc. silently go missing from the
    staged Python and the shipped installer is broken on any clean machine.

    Setting ``PYTHONNOUSERSITE=1`` removes the per-user site directory from
    ``sys.path`` for these subprocesses, so pip resolves every requirement
    against the (empty) embedded tree and installs the full closure there,
    making the staged Python genuinely self-contained.
    """
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    return env


def download(url: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` if not already present."""
    if dest.exists():
        log(f"already downloaded: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as f:  # noqa: S310 — pinned URLs
        shutil.copyfileobj(resp, f)
    return dest


def unzip(zip_path: Path, dest: Path) -> None:
    log(f"unzipping {zip_path.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def stage_embedded_python() -> Path:
    """Download and configure the embedded Python distribution."""
    target = STAGING / "python"
    if target.exists():
        shutil.rmtree(target)

    zip_path = download(
        PYTHON_EMBED_URL,
        DOWNLOADS / f"python-{PYTHON_VERSION}-embed-amd64.zip",
    )
    unzip(zip_path, target)

    # Edit pythonXY._pth to allow site-packages. The embedded distribution
    # ships with `import site` commented out — uncomment it so pip-installed
    # packages in Lib\site-packages are importable.
    pth_files = list(target.glob("python3*._pth"))
    if not pth_files:
        raise RuntimeError(f"no python3*._pth file found in {target}")
    pth = pth_files[0]
    content = pth.read_text()
    content = content.replace("#import site", "import site")
    # Also add Lib\site-packages explicitly. The embedded distribution
    # doesn't include this path by default even with `import site` enabled.
    if "Lib\\site-packages" not in content and "Lib/site-packages" not in content:
        content = content.rstrip() + "\nLib\\site-packages\n"
    pth.write_text(content)

    # Bootstrap pip
    get_pip = download(GET_PIP_URL, DOWNLOADS / "get-pip.py")
    log("bootstrapping pip into embedded Python")
    subprocess.run(
        [str(target / "python.exe"), str(get_pip), "--no-warn-script-location"],
        check=True,
        env=_embedded_env(),
    )

    # Install setuptools + wheel. The embedded distribution ships without
    # them, and get-pip.py installs only pip — but pip needs
    # ``setuptools.build_meta`` (PEP 517 backend) to build any source-only
    # package. Bambuddy's requirements.txt hits this with pyftpdlib 2.2.0
    # which is sdist-only on PyPI; other source-only packages would fail
    # the same way without this step.
    log("installing setuptools + wheel for PEP 517 builds")
    subprocess.run(
        [
            str(target / "python.exe"),
            "-m",
            "pip",
            "install",
            "--no-warn-script-location",
            "setuptools",
            "wheel",
        ],
        check=True,
        env=_embedded_env(),
    )

    return target


def install_requirements(python_dir: Path) -> None:
    """Install Bambuddy's requirements.txt into the embedded Python."""
    py = python_dir / "python.exe"
    requirements = REPO_ROOT / "requirements.txt"
    log(f"installing requirements.txt into {python_dir}")
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-warn-script-location",
            "-r",
            str(requirements),
        ],
        check=True,
        env=_embedded_env(),
    )


def build_frontend() -> Path:
    """Run ``npm ci && npm run build`` and return the build output path.

    Vite is configured with ``outDir: '../static'`` (see
    ``frontend/vite.config.ts``), so the bundle lands at ``<repo>/static/``
    — NOT ``frontend/dist/``. The path matches the runtime expectation in
    ``backend/app/core/config.py`` (``static_dir = _app_dir / "static"``).
    """
    frontend = REPO_ROOT / "frontend"
    dist = REPO_ROOT / "static"
    log("running npm ci in frontend/")
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found on PATH — install Node.js 22 LTS")
    subprocess.run([npm, "ci"], cwd=frontend, check=True, shell=False)
    log("running npm run build in frontend/")
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True, shell=False)
    if not dist.exists():
        raise RuntimeError(f"expected frontend build output at {dist}")
    return dist


def stage_backend(frontend_dist: Path) -> None:
    """Copy backend source + frontend bundle into the staging tree.

    The runtime layout under STAGING/app/ mirrors a Bambuddy checkout:
    ``backend/`` (source), ``static/`` (frontend bundle served by FastAPI).
    """
    app = STAGING / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)

    # Backend source — copy the package tree, skip caches/tests/migrations
    log("staging backend source")
    shutil.copytree(
        REPO_ROOT / "backend",
        app / "backend",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "tests",
            ".pytest_cache",
        ),
    )

    # Frontend bundle — FastAPI's StaticFiles mounts from app/static.
    # Strip macOS metadata files (.DS_Store, ._.*) that the dev box leaks
    # in; they'd just bloat the installer and never be served anyway.
    log("staging frontend bundle")
    shutil.copytree(
        frontend_dist,
        app / "static",
        ignore=shutil.ignore_patterns(".DS_Store", "._*"),
    )

    # gcode_viewer/ is a vendored 3D-preview iframe served via explicit
    # routes in main.py (looked up via static_dir.parent / "gcode_viewer").
    # In the staged layout STAGING/app/static/'s sibling is STAGING/app/,
    # so place the directory next to static/ to match runtime resolution.
    gcode_viewer_src = REPO_ROOT / "gcode_viewer"
    if gcode_viewer_src.exists():
        log("staging gcode_viewer/")
        shutil.copytree(
            gcode_viewer_src,
            app / "gcode_viewer",
            ignore=shutil.ignore_patterns(".DS_Store", "._*"),
        )


def stage_nssm() -> None:
    target = STAGING / "bin"
    target.mkdir(parents=True, exist_ok=True)
    # Vendored binary — no network fetch at build time
    src = INSTALLER_DIR / "vendor" / "nssm.exe"
    if not src.exists():
        raise RuntimeError(f"vendored NSSM binary missing at {src} — was it committed?")
    log(f"staging nssm.exe from {src}")
    shutil.copy(src, target / "nssm.exe")


def stage_ffmpeg() -> None:
    target = STAGING / "bin"
    target.mkdir(parents=True, exist_ok=True)
    zip_path = download(FFMPEG_URL, DOWNLOADS / "ffmpeg-win64-gpl.zip")
    extract = DOWNLOADS / "ffmpeg-extracted"
    if not extract.exists():
        unzip(zip_path, extract)
    src = next(extract.rglob("bin/ffmpeg.exe"))
    log(f"staging ffmpeg.exe from {src}")
    shutil.copy(src, target / "ffmpeg.exe")
    # ffprobe is used by some camera/timelapse paths
    ffprobe = next(extract.rglob("bin/ffprobe.exe"), None)
    if ffprobe is not None:
        shutil.copy(ffprobe, target / "ffprobe.exe")


def stage_service_scripts() -> None:
    """Copy the service install/uninstall .bat files into staging."""
    service_src = INSTALLER_DIR / "service"
    service_dst = STAGING / "service"
    if service_dst.exists():
        shutil.rmtree(service_dst)
    shutil.copytree(service_src, service_dst)


def stage_erp_config() -> None:
    """Optionally bundle the ERP deploy-secrets file (``erp.env``).

    If ``installers/windows/erp.env`` exists it is copied into the staged
    ``config/`` tree so the installer lays it down at
    ``C:\\ProgramData\\Bambuddy\\config\\erp.env`` — read at runtime to
    pre-configure ERP directory login with zero post-install steps.

    The file is OPTIONAL: a build without it must still succeed (the .iss
    [Files] entry uses ``skipifsourcedoesntexist``), so this NEVER fails. A
    stale copy from a previous build is removed when the source is absent so
    an unrelated build never ships someone else's secrets.
    """
    src = INSTALLER_DIR / "erp.env"
    dst = STAGING / "config" / "erp.env"
    if not src.exists():
        if dst.exists():
            dst.unlink()
        log("no erp.env present — ERP login not pre-configured in this build")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"staging erp.env -> {dst}")
    shutil.copy(src, dst)


def _read_app_version() -> str:
    """Read APP_VERSION from backend/app/core/config.py (the canonical
    source used by every other Bambuddy surface — FastAPI OpenAPI title,
    /system info, support bundles, spoolbuddy update check).
    """
    config_py = REPO_ROOT / "backend" / "app" / "core" / "config.py"
    if not config_py.exists():
        return "0.0.0+dev"
    for raw in config_py.read_text().splitlines():
        stripped = raw.strip()
        if stripped.startswith("APP_VERSION"):
            # APP_VERSION = "0.2.5b1"  ->  0.2.5b1
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0+dev"


def _git_short_sha() -> str:
    """Return the short git SHA of HEAD, or ``""`` if git is unavailable.

    Used only to make LOCAL build IDs traceable to a commit. Any failure
    (git not installed, not a repo, detached/empty state) degrades to an
    empty string — the timestamp alone still uniquely stamps the build.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _resolve_installer_version() -> str:
    """Decide what version string the installer carries.

    Priority:
      1. ``GITHUB_REF`` env var when set to a tag (e.g.
         ``refs/tags/v0.2.5b1-daily.20260610``) — the daily-beta and stable
         publish scripts both push tags in the ``v<APP_VERSION>[-daily.<date>]``
         shape, and we want the installer filename + Inno Setup AppVersion
         to match the GitHub release exactly so dailies stay distinguishable
         from each other and from the eventual stable.
      2. Local builds: ``APP_VERSION`` + a unique ``+<build id>`` suffix so a
         stale exe left on disk is never mistaken for a fresh one. The build
         id is a UTC timestamp plus the short git SHA (SHA omitted when git
         is unavailable), e.g. ``0.2.4.8+20260713142530.5ac21b9c``.

    Strips the leading ``v`` from tags so the installer filename is
    ``bambuddy-0.2.5b1-daily.20260610-windows-x64-setup.exe``, not
    ``bambuddy-v0.2.5b1-...``.
    """
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if tag.startswith("v"):
            tag = tag[1:]
        return tag or _read_app_version()

    base = _read_app_version()
    build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    sha = _git_short_sha()
    if sha:
        build_id = f"{build_id}.{sha}"
    return f"{base}+{build_id}"


def write_version_file() -> None:
    """Write the installer version as both a plain VERSION file and an
    Inno Setup include file so the .iss script can pick it up at compile
    time without a fragile file-read hack.

    The plain VERSION file is staged INSIDE the app tree (``staging/app``) so
    the recursive ``{app}\\app`` copy ships it to ``{app}\\app\\VERSION`` — the
    location the backend reads at runtime (``config._resolve_build_version``)
    to report the deployed build. This runs after the app tree is staged, so
    the file survives.
    """
    version = _resolve_installer_version()
    (STAGING / "app" / "VERSION").write_text(version)

    # Inno Setup's VersionInfoVersion (the binary FILEVERSION resource) must be
    # a plain numeric x.x.x.x — it can't carry the local ``+<build id>`` suffix.
    # Emit it from the canonical APP_VERSION base with any ``+suffix`` stripped,
    # while MyAppVersion keeps the full (unique) string for the filename +
    # displayed AppVersion.
    numeric = _read_app_version().split("+", 1)[0]

    # Inno Setup include — bambuddy.iss does `#include "build\staging\version.iss"`
    iss_version = STAGING / "version.iss"
    iss_version.write_text(
        f'#define MyAppVersion "{version}"\n'
        f'#define MyAppVersionInfo "{numeric}"\n'
    )
    log(f"staged VERSION = {version} (VersionInfo {numeric})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip frontend build (use existing frontend/dist/)",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skip pip install (use existing staged Python)",
    )
    parser.add_argument(
        "--allow-non-windows",
        action="store_true",
        help=(
            "Override the Windows-only guard. Only useful if you have a "
            "working wine + windows-python toolchain. Not exercised by CI."
        ),
    )
    args = parser.parse_args()

    if sys.platform != "win32" and not args.allow_non_windows:
        log("ERROR: this build script must run on Windows.")
        log("")
        log("It downloads a Windows embeddable Python distribution and")
        log("pip-installs Bambuddy's requirements.txt against it — both")
        log("require executing python.exe, which only runs on Windows.")
        log("")
        log("Supported build paths:")
        log("  1. GitHub Actions: trigger '.github/workflows/windows-")
        log("     installer.yml' (Actions tab -> Windows Installer ->")
        log("     Run workflow). Downloads the .exe as a workflow artifact.")
        log("  2. Windows VM / box: clone, install Python 3.13 + Node 22 +")
        log("     Inno Setup 6, run this script.")
        log("")
        log("Unsupported escape hatch (cross-build under Wine): rerun with")
        log("--allow-non-windows. Requires wine + a Windows Python in $PATH")
        log("via wine python.exe — fragile and not exercised by CI.")
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)

    python_dir = stage_embedded_python()
    if not args.skip_pip:
        install_requirements(python_dir)

    if args.skip_frontend:
        frontend_dist = REPO_ROOT / "frontend" / "dist"
        if not frontend_dist.exists():
            raise RuntimeError("--skip-frontend given but frontend/dist/ doesn't exist")
    else:
        frontend_dist = build_frontend()

    stage_backend(frontend_dist)
    stage_nssm()
    stage_ffmpeg()
    stage_service_scripts()
    stage_erp_config()
    write_version_file()

    log("")
    log("=" * 60)
    log("Staging complete.")
    log(f"Staged tree: {STAGING}")
    log("")
    log("Next: compile the Inno Setup script:")
    log('  "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" bambuddy.iss')
    log("")
    log(f"Installer will be written to: {BUILD_DIR / 'output'}")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

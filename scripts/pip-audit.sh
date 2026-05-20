#!/usr/bin/env bash
# Local pip-audit wrapper — mirrors the ignore list in
# .github/workflows/security.yml so `./scripts/pip-audit.sh` matches what CI sees.
# Keep both sides in sync when adding or removing ignores.
#
# CVE-2025-45768 (PYSEC-2025-183 / GHSA-65pc-fj4g-8rjx): disputed by PyJWT
# maintainers; no fix version exists. Bambuddy uses secrets.token_urlsafe(64)
# (~86 chars) and rejects file-loaded secrets shorter than 32 chars
# (backend/app/core/auth.py:177, :184). Safe to ignore permanently.
set -euo pipefail
source /opt/claude/projects/bambuddy/venv/bin/activate

exec pip-audit \
  --ignore-vuln CVE-2025-45768 \
  "$@"

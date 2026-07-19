"""Shared claim/role -> Bambuddy-group mapping for provider-managed logins.

Single owner of the "replace a user's group memberships from an external
identity's claims" operation. Called by BOTH:
  - the OIDC callback (claim-value -> group name, per-provider config), and
  - the ERP directory login (role -> group name, per-settings config).

Keeping one implementation means the membership-replace + audit-log behavior
is identical across both identity sources.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.group import Group
from backend.app.models.user import User
from backend.app.services.ldap_service import resolve_group_mapping

logger = logging.getLogger(__name__)


def _normalize_claim_values(raw) -> list[str]:
    """Normalize an OIDC claim value to a list of strings.

    A groups claim may arrive as a single string ("admins") or a list
    (["admins", "ops"]). Non-string members are dropped; everything else
    (dict, number, None) yields an empty list.
    """
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()]
    return []


def resolve_claim_groups(provider, claims: dict) -> list[str] | None:
    """Resolve the Bambuddy group names an OIDC user should map to.

    Returns:
      - None  when the provider has no group mapping configured OR the groups
              claim is absent from the token (=> caller leaves groups as-is).
      - list  of mapped Bambuddy group names otherwise; may be empty when the
              claim is present but no value matched the mapping.
    """
    groups_claim = getattr(provider, "groups_claim", None)
    group_mapping_raw = getattr(provider, "group_mapping", None)
    if not groups_claim or not group_mapping_raw:
        return None

    raw = claims.get(groups_claim)
    if raw is None:
        # Claim not present in the token -> caller leaves groups untouched.
        return None

    values = _normalize_claim_values(raw)
    if not values:
        return None

    try:
        mapping = json.loads(group_mapping_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(mapping, dict):
        return None

    return resolve_group_mapping(values, mapping)


async def apply_group_mapping(
    db: AsyncSession,
    user: User,
    mapped_group_names: list[str],
    *,
    source: str,
) -> bool:
    """Replace ``user``'s group memberships with the groups named in
    ``mapped_group_names`` (resolved to Group rows by name).

    Returns True when the membership set actually changed (caller commits),
    False when it was already in sync. Logs the old -> new transition tagged
    with ``source`` (e.g. "erp", "oidc").

    The user must already have its ``groups`` collection loaded (selectinload)
    — every caller loads it that way — so no async lazy-load fires here.
    """
    new_groups: list[Group] = []
    if mapped_group_names:
        result = await db.execute(select(Group).where(Group.name.in_(mapped_group_names)))
        new_groups = list(result.scalars().all())

    # Silent-deprovision observability (D12): the identity source named one or
    # more roles/groups, but NONE resolved to a local Bambuddy group. The
    # fail-closed replace below then clears the user's memberships entirely —
    # a legitimate but easy-to-miss lockout usually caused by a group-mapping
    # typo or a group renamed/deleted on our side. Surface it loudly (no
    # semantic change — the replace still happens).
    if mapped_group_names and not new_groups:
        logger.warning(
            "[group-sync:%s] ERP/OIDC roles %s matched no local group — user %s memberships will be cleared",
            source,
            sorted(mapped_group_names),
            user.username,
        )

    old_ids = {g.id for g in user.groups}
    new_ids = {g.id for g in new_groups}
    if old_ids == new_ids:
        return False

    old_names = sorted(g.name for g in user.groups)
    user.groups = new_groups
    logger.info(
        "[group-sync:%s] %s: %s -> %s",
        source,
        user.username,
        old_names,
        sorted(g.name for g in new_groups),
    )
    return True

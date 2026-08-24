from datetime import datetime, timezone
from typing import Annotated

import jwt as _jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_external_login_url
from backend.app.core.auth import (
    ALGORITHM,
    SECRET_KEY,
    RequireAdminIfAuthEnabled,
    RequirePermissionIfAuthEnabled,
    get_current_user_optional,
    get_password_hash,
    revoke_jti,
    security,
    verify_password,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.auth import (
    ChangePasswordRequest,
    GroupBrief,
    UserCreate,
    UserDeleteImpact,
    UserResponse,
    UserUpdate,
)
from backend.app.services import user_deletion
from backend.app.services.email_service import (
    create_welcome_email_from_template,
    generate_secure_password,
    get_smtp_settings,
    send_email,
)

router = APIRouter(prefix="/users", tags=["users"])


def _user_to_response(user: User) -> UserResponse:
    """Convert a User model to UserResponse schema."""
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_admin=user.is_admin,
        auth_source=getattr(user, "auth_source", "local"),
        groups=[GroupBrief(id=g.id, name=g.name) for g in user.groups],
        permissions=sorted(user.get_permissions()),
        created_at=user.created_at.isoformat(),
    )


@router.get("", response_model=list[UserResponse])
@router.get("/", response_model=list[UserResponse])
async def list_users(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """List all users.

    Read-only — gated on ``USERS_READ`` only. Operator-visible UIs
    (Stats filter-by-user, Archives Print Log username column, File
    Manager username autocomplete) consume this endpoint via custom-
    group ``users:read`` grants without admin role. The admin-only
    boundary lives on the write endpoints below."""
    result = await db.execute(select(User).options(selectinload(User.groups)).order_by(User.created_at))
    users = result.scalars().all()
    return [_user_to_response(user) for user in users]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    _admin: User | None = RequireAdminIfAuthEnabled(),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_CREATE),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user.

    When advanced authentication is enabled:
    - Email is required
    - Password is auto-generated and emailed to user
    - Admin cannot set or see the password
    """
    import logging

    logger = logging.getLogger(__name__)

    # Check if advanced auth is enabled
    result = await db.execute(select(Settings).where(Settings.key == "advanced_auth_enabled"))
    advanced_auth_setting = result.scalar_one_or_none()
    advanced_auth_enabled = advanced_auth_setting and advanced_auth_setting.value.lower() == "true"

    # Check if username already exists (case-insensitive)
    existing_user = await db.execute(select(User).where(func.lower(User.username) == func.lower(user_data.username)))
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    # Validate role
    if user_data.role not in ["admin", "user"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be 'admin' or 'user'",
        )

    # Advanced auth validation
    if advanced_auth_enabled:
        if not user_data.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required when advanced authentication is enabled",
            )
        # Check if email already exists (case-insensitive)
        existing_email = await db.execute(select(User).where(func.lower(User.email) == func.lower(user_data.email)))
        if existing_email.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists",
            )

    # Generate password if advanced auth enabled, otherwise require password
    if advanced_auth_enabled:
        password = generate_secure_password()
    else:
        if not user_data.password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is required when advanced authentication is disabled",
            )
        password = user_data.password

    new_user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=get_password_hash(password),
        role=user_data.role,
        is_active=True,
    )

    # Handle group assignments
    if user_data.group_ids:
        groups_result = await db.execute(select(Group).where(Group.id.in_(user_data.group_ids)))
        groups = groups_result.scalars().all()
        if len(groups) != len(user_data.group_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more group IDs are invalid",
            )
        new_user.groups = list(groups)

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    # Send welcome email if advanced auth enabled
    if advanced_auth_enabled and new_user.email:
        try:
            smtp_settings = await get_smtp_settings(db)
            if smtp_settings:
                login_url = await get_external_login_url(db)
                subject, text_body, html_body = await create_welcome_email_from_template(
                    db, new_user.username, password, login_url
                )
                send_email(smtp_settings, new_user.email, subject, text_body, html_body)
                logger.info(f"Welcome email sent to {new_user.email}")
            else:
                logger.warning(f"SMTP not configured, could not send welcome email to {new_user.email}")
        except Exception as e:
            logger.error(f"Failed to send welcome email: {e}")
            # Don't fail user creation if email fails

    return _user_to_response(new_user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Get a user by ID. Read-only — gated on ``USERS_READ`` only."""
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return _user_to_response(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    _admin: User | None = RequireAdminIfAuthEnabled(),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Update a user."""
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Prevent deactivating the last admin
    if user_data.is_active is False and user.is_admin:
        # Count admins by role or Administrators group membership
        admin_count_result = await db.execute(select(User).where(User.role == "admin", User.is_active.is_(True)))
        role_admins = admin_count_result.scalars().all()

        # Also check for users in Administrators group
        admin_group_result = await db.execute(
            select(Group).where(Group.name == "Administrators").options(selectinload(Group.users))
        )
        admin_group = admin_group_result.scalar_one_or_none()
        group_admins = [u for u in (admin_group.users if admin_group else []) if u.is_active]

        # Combine unique admins
        all_admins = {u.id for u in role_admins} | {u.id for u in group_admins}
        if len(all_admins) <= 1 and user.id in all_admins:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last admin user",
            )

    # Prevent changing role of last admin
    if user_data.role and user_data.role != "admin" and user.role == "admin":
        admin_count_result = await db.execute(select(User).where(User.role == "admin", User.is_active.is_(True)))
        admin_count = len(admin_count_result.scalars().all())
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change role of the last admin user",
            )

    if user_data.username is not None:
        # Check if new username already exists (case-insensitive)
        existing_user = await db.execute(
            select(User).where(func.lower(User.username) == func.lower(user_data.username), User.id != user_id)
        )
        if existing_user.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists",
            )
        user.username = user_data.username

    if user_data.email is not None:
        # Check if new email already exists (case-insensitive)
        existing_email = await db.execute(
            select(User).where(func.lower(User.email) == func.lower(user_data.email), User.id != user_id)
        )
        if existing_email.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists",
            )
        user.email = user_data.email

    if user_data.password is not None:
        if getattr(user, "auth_source", "local") == "ldap":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot set password for LDAP users",
            )
        user.password_hash = get_password_hash(user_data.password)

    if user_data.role is not None:
        if user_data.role not in ["admin", "user"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role must be 'admin' or 'user'",
            )
        user.role = user_data.role

    if user_data.is_active is not None:
        user.is_active = user_data.is_active

    # Handle group assignments
    if user_data.group_ids is not None:
        groups_result = await db.execute(select(Group).where(Group.id.in_(user_data.group_ids)))
        groups = groups_result.scalars().all()
        if len(groups) != len(user_data.group_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more group IDs are invalid",
            )
        user.groups = list(groups)

    await db.commit()
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one()

    return _user_to_response(user)


@router.get("/{user_id}/delete-impact", response_model=UserDeleteImpact)
async def get_user_delete_impact(
    user_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
) -> UserDeleteImpact:
    """Pre-flight for the delete-confirm dialog. Read-only — gated on ``USERS_READ``.

    Shaped after ``GET /archives/{id}/delete-impact`` and for the same reason: one
    cheap dedicated endpoint, rather than making the much larger user LIST run
    these counts per row. The counts themselves are ``services.user_deletion``'s,
    so the dialog and the delete describe the same estate.

    Replaces ``items-count``, which reported three of these six numbers and got
    one of them wrong — it filtered out soft-deleted library files, which the
    delete destroys along with everything else, and it said nothing about SKUs or
    about live prints that would refuse the request outright.
    """
    result = await db.execute(select(User.id).where(User.id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return await user_deletion.delete_impact(db, user_id=user_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    delete_items: bool = Query(False, description="Delete all items created by this user"),
    _admin: User | None = RequireAdminIfAuthEnabled(),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.USERS_DELETE),
    db: AsyncSession = Depends(get_db),
):
    """Delete a user, and with ``delete_items=true`` everything they created.

    The route decides only whether this user MAY be deleted — they exist, they are
    not the last admin, they are not the caller. The deletion itself is
    ``services.user_deletion.delete_user``: which rows go, which are merely
    disowned, which dependent rows the engine will not cascade for us, when the
    bytes leave disk, and the wholesale refusal that protects a live print. None of
    that is routing, and while it lived here it was a list of raw multi-table
    DELETEs whose guard asked a narrower question than its statements did.

    ``delete_items=true`` is refused WHOLESALE with a structured 409
    (``user_has_printing_units``) while ANY live print is running off a row in
    scope — the user's own queued units, or another operator's print running off
    this user's archive or library file. Nothing is deleted on that path.
    """
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Prevent deleting the last admin
    if user.is_admin:
        # Count admins by role or Administrators group membership
        admin_count_result = await db.execute(select(User).where(User.role == "admin", User.id != user_id))
        other_role_admins = admin_count_result.scalars().all()

        # Also check for users in Administrators group
        admin_group_result = await db.execute(
            select(Group).where(Group.name == "Administrators").options(selectinload(Group.users))
        )
        admin_group = admin_group_result.scalar_one_or_none()
        other_group_admins = [u for u in (admin_group.users if admin_group else []) if u.id != user_id and u.is_active]

        # Combine unique admins
        all_other_admins = {u.id for u in other_role_admins} | {u.id for u in other_group_admins}
        if len(all_other_admins) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin user",
            )

    # Prevent deleting yourself (only if auth is enabled and we have a current user)
    if current_user and user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    await user_deletion.delete_user(db, user=user, delete_items=delete_items)


@router.post("/me/change-password", response_model=dict)
async def change_own_password(
    password_data: ChangePasswordRequest,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's password. Requires current password verification."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to change password",
        )

    # Block password change for LDAP users
    if getattr(current_user, "auth_source", "local") == "ldap":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change password for LDAP users — passwords are managed by the LDAP server",
        )

    # Block password change for ERP users BEFORE the hash check: unlike OIDC
    # users (password_hash=None), ERP users carry a mirrored bcrypt hash and
    # would otherwise fall through and overwrite the mirror with a local
    # pbkdf2 hash — desyncing the offline credential cache from the ERP.
    if getattr(current_user, "auth_source", "local") == "erp":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change password for ERP users — passwords are managed in the Foundi ERP",
        )

    # Verify current password
    if not current_user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account has no local password set",
        )

    # Rate-limit failed password-change attempts (H-R5-A)
    from backend.app.api.routes.mfa import MAX_2FA_ATTEMPTS, check_rate_limit, record_failed_attempt

    await check_rate_limit(db, current_user.username, event_type="password_change", max_attempts=MAX_2FA_ATTEMPTS)

    if not verify_password(password_data.current_password, current_user.password_hash):
        await record_failed_attempt(db, current_user.username, event_type="password_change")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    # Fetch user from this session to ensure changes are persisted
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update password
    user.password_hash = get_password_hash(password_data.new_password)
    user.password_changed_at = datetime.now(timezone.utc)  # M-R7-B: invalidate all prior JWTs
    await db.commit()

    # L-R6-A: Password verified successfully — reset the failure counter
    from backend.app.api.routes.mfa import clear_failed_attempts

    await clear_failed_attempts(db, user.username, event_type="password_change")

    # Revoke the current session token so the caller must re-authenticate (M-R5-A)
    if credentials is not None:
        try:
            payload = _jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                try:
                    await revoke_jti(jti, datetime.fromtimestamp(exp, tz=timezone.utc), user.username)
                except Exception as exc:
                    # B4: log so operators know revocation is broken; password was
                    # already changed so the token will fail freshness checks anyway.
                    import logging

                    logging.getLogger(__name__).error(
                        "Failed to revoke JTI after password change for user %s: %s", user.username, exc
                    )
        except Exception:
            pass  # Decode failure is harmless — token is already invalidated by password_changed_at

    return {"message": "Password changed successfully"}

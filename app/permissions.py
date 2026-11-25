"""All permission rules in one place. Change the mapping to change who can do what."""
from __future__ import annotations

ADMIN = "admin"
USER = "user"  # any logged-in, enabled user

PERMISSIONS: dict[str, str] = {
    "view_entries": USER,
    "edit_entries": USER,  # create, edit, delete, duplicate, snooze, renew
    "export": USER,
    "view_notification_setup": USER,
    "change_own_password": USER,
    "manage_columns": ADMIN,  # add, remove, rename, reorder, change type/options
    "manage_users": ADMIN,
    "manage_settings": ADMIN,  # send time, timezone, thresholds, test notification
}


def can(user, permission: str) -> bool:
    """`user` is a mapping/row with `is_admin` and `disabled`, or None."""
    if user is None or user["disabled"]:
        return False
    required = PERMISSIONS.get(permission)
    if required is None:
        return False
    if required == USER:
        return True
    return bool(user["is_admin"])

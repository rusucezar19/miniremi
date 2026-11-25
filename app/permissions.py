"""All permission rules in one place. Change the mapping to change who can do what."""
from __future__ import annotations

ADMIN = "admin"
USER = "user"  # any logged-in, enabled user
NOBODY = "nobody"  # switched off for everyone

PERMISSIONS: dict[str, str] = {
    "view_entries": USER,  # own entries only
    "edit_entries": USER,  # create, edit, delete, duplicate, snooze, renew (own entries only)
    # See and edit other users' entries too. Set to ADMIN to give admins an overview of everything.
    # Reminders always go only to the entry's owner, whatever this is set to.
    "view_all_entries": NOBODY,
    "export": USER,
    "import_entries": USER,  # CSV import; imported entries belong to the importing user
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
    if required is None or required == NOBODY:
        return False
    if required == USER:
        return True
    return bool(user["is_admin"])


def entry_scope(user) -> int | None:
    """Owner id to filter entries by, or None if this user may see everyone's entries."""
    return None if can(user, "view_all_entries") else user["id"]

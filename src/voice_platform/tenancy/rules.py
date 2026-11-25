"""Who may grant, change or remove which role. Pure functions, unit-tested exhaustively.

Rules:
- Only an OWNER can grant the OWNER role or change/remove another OWNER.
- Anyone else can only act on members ranked strictly below them, and can only grant
  roles at or below their own rank.
- Nobody changes their own role (ask another admin). Leaving is a separate action.
- An organization always keeps at least one OWNER.
"""

from __future__ import annotations

from voice_platform.tenancy.permissions import ROLE_RANK, Role


class MembershipRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_can_grant(actor: Role, new_role: Role) -> None:
    if new_role is Role.OWNER and actor is not Role.OWNER:
        raise MembershipRuleError("role_too_high", "Only an owner can grant the owner role.")
    if ROLE_RANK[new_role] > ROLE_RANK[actor]:
        raise MembershipRuleError("role_too_high", "You can't grant a role higher than your own.")


def check_can_manage(actor: Role, target: Role) -> None:
    if actor is Role.OWNER:
        return
    if ROLE_RANK[target] >= ROLE_RANK[actor]:
        raise MembershipRuleError(
            "target_too_high", "You can only manage members with a lower role than yours."
        )


def check_role_change(
    *, actor: Role, target: Role, new_role: Role, is_self: bool, owner_count: int
) -> None:
    if is_self:
        raise MembershipRuleError("cannot_change_own_role", "You can't change your own role.")
    check_can_manage(actor, target)
    check_can_grant(actor, new_role)
    if target is Role.OWNER and new_role is not Role.OWNER and owner_count <= 1:
        raise MembershipRuleError("last_owner", "An organization needs at least one owner.")


def check_removal(*, actor: Role, target: Role, is_self: bool, owner_count: int) -> None:
    if target is Role.OWNER and owner_count <= 1:
        raise MembershipRuleError("last_owner", "An organization needs at least one owner.")
    if not is_self:
        check_can_manage(actor, target)

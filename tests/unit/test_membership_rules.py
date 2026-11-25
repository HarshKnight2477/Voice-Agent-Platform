from __future__ import annotations

import pytest

from voice_platform.tenancy.permissions import ROLE_RANK, Role
from voice_platform.tenancy.rules import (
    MembershipRuleError,
    check_can_grant,
    check_removal,
    check_role_change,
)

ALL = list(Role)


@pytest.mark.parametrize("actor", ALL)
@pytest.mark.parametrize("new_role", ALL)
def test_grant_never_exceeds_own_rank(actor: Role, new_role: Role) -> None:
    allowed = ROLE_RANK[new_role] <= ROLE_RANK[actor]
    if allowed:
        check_can_grant(actor, new_role)
    else:
        with pytest.raises(MembershipRuleError):
            check_can_grant(actor, new_role)


@pytest.mark.parametrize("actor", [r for r in ALL if r is not Role.OWNER])
def test_non_owner_cannot_manage_equal_or_higher(actor: Role) -> None:
    for target in ALL:
        if ROLE_RANK[target] >= ROLE_RANK[actor]:
            with pytest.raises(MembershipRuleError, match="lower role"):
                check_role_change(
                    actor=actor, target=target, new_role=Role.VIEWER, is_self=False, owner_count=2
                )


def test_owner_can_demote_another_owner_but_not_the_last() -> None:
    check_role_change(
        actor=Role.OWNER, target=Role.OWNER, new_role=Role.ADMIN, is_self=False, owner_count=2
    )
    with pytest.raises(MembershipRuleError) as exc:
        check_role_change(
            actor=Role.OWNER, target=Role.OWNER, new_role=Role.ADMIN, is_self=False, owner_count=1
        )
    assert exc.value.code == "last_owner"


def test_nobody_changes_their_own_role() -> None:
    with pytest.raises(MembershipRuleError) as exc:
        check_role_change(
            actor=Role.OWNER, target=Role.OWNER, new_role=Role.VIEWER, is_self=True, owner_count=3
        )
    assert exc.value.code == "cannot_change_own_role"


def test_admin_cannot_grant_owner() -> None:
    with pytest.raises(MembershipRuleError, match="owner"):
        check_role_change(
            actor=Role.ADMIN, target=Role.VIEWER, new_role=Role.OWNER, is_self=False, owner_count=1
        )


def test_last_owner_cannot_leave_but_others_can() -> None:
    with pytest.raises(MembershipRuleError):
        check_removal(actor=Role.OWNER, target=Role.OWNER, is_self=True, owner_count=1)
    check_removal(actor=Role.OWNER, target=Role.OWNER, is_self=True, owner_count=2)
    check_removal(actor=Role.VIEWER, target=Role.VIEWER, is_self=True, owner_count=1)


def test_manager_can_remove_analyst_but_not_admin() -> None:
    check_removal(actor=Role.MANAGER, target=Role.ANALYST, is_self=False, owner_count=1)
    with pytest.raises(MembershipRuleError):
        check_removal(actor=Role.MANAGER, target=Role.ADMIN, is_self=False, owner_count=1)

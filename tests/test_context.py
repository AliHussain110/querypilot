from app.context import MOCK_USERS, Role


def test_only_admins_can_see_sql() -> None:
    assert MOCK_USERS["alice"].can_see_sql
    assert not MOCK_USERS["bob"].can_see_sql


def test_mock_users_cover_roles_and_multiple_orgs() -> None:
    assert {u.role for u in MOCK_USERS.values()} == {Role.ADMIN, Role.USER}
    assert len({u.org_id for u in MOCK_USERS.values()}) >= 3

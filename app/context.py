"""Who is asking: a mock identity with a role and an organization (no real auth)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True)
class UserContext:
    name: str
    role: Role
    org_id: int
    org_name: str

    @property
    def can_see_sql(self) -> bool:
        """Admins may see generated SQL; normal users only get answers and tables."""
        return self.role is Role.ADMIN


# Demo identities matching the seeded organizations (1 Acme, 2 Globex, 3 Initech).
MOCK_USERS: dict[str, UserContext] = {
    "alice": UserContext("Alice", Role.ADMIN, 1, "Acme Corp"),
    "bob": UserContext("Bob", Role.USER, 1, "Acme Corp"),
    "grace": UserContext("Grace", Role.ADMIN, 2, "Globex Industries"),
    "ivan": UserContext("Ivan", Role.USER, 3, "Initech Labs"),
}

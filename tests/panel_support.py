"""The panel's API on the embedded database, signed in as a seeded user.

Shared by every ``test_panel_*`` module. Authentication is stubbed --
it has its own suite -- while authorisation and row-level security are not:
the role checks and the RLS binding both run for real, because they are
what these tests exist to exercise.

Seeded users rather than invented ids: campaigns, documents and centres
record who created them, and the audit chain is about real actors. A role
the seed does not ship gets a fresh id, which is enough for a refusal.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from uaagro_db.engine import bind_rls_context
from uaagro_db.models import Organization, User
from uaagro_domain.enums import Role


@asynccontextmanager
async def panel_client(app_engine: Any) -> AsyncIterator[AsyncClient]:
    from api.main import app
    from api.security import deps

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    state: dict[str, Any] = {}

    async with maker() as session:
        state["org_id"] = await session.scalar(select(Organization.id).limit(1))
        users = (await session.execute(select(User.id, User.role))).all()
    state["users"] = {role: user_id for user_id, role in users}

    async def fake_principal() -> deps.Principal:
        return state["principal"]  # type: ignore[no-any-return]

    async def fake_db() -> AsyncIterator[Any]:
        async with maker() as session:
            principal = state["principal"]
            await bind_rls_context(
                session,
                user_id=principal.user_id,
                role=principal.role.value,
                centre_ids=principal.centre_ids,
                org_id=principal.organization_id,
            )
            yield session
            await session.commit()

    app.dependency_overrides[deps.current_principal] = fake_principal
    app.dependency_overrides[deps.scoped_db] = fake_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            http.state = state  # type: ignore[attr-defined]
            yield http
    finally:
        app.dependency_overrides.clear()


def act_as(http: AsyncClient, role: Role, *, centres: tuple[uuid.UUID, ...] = ()) -> uuid.UUID:
    """Run the next requests as the seeded user with this role."""
    from api.security.deps import Principal

    state = http.state  # type: ignore[attr-defined]
    user_id: uuid.UUID = state["users"].get(role) or uuid.uuid4()
    state["principal"] = Principal(
        user_id=user_id,
        organization_id=state["org_id"],
        role=role,
        centre_ids=centres,
        session_id=uuid.uuid4(),
    )
    return user_id


__all__ = ("act_as", "panel_client")

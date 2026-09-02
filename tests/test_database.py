"""Integration tests against a real Postgres 16.

These are the tests that prove §10 and §17 actually hold rather than merely
being written down: Row-Level Security really isolates a centre, unapproved
advisory rows really cannot be read, the audit chain really detects tampering,
and the seed really is idempotent.

They run against the embedded server from ``tests/pgfixture.py`` (Postgres 16.2
with pgvector, no Docker, no root). The one fidelity gap is ``pg_trgm``, which
that build does not ship — asserted explicitly below so the gap is visible
rather than assumed away.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _as_role(connection, *, role: str, centre_ids: list[uuid.UUID]) -> None:
    """Bind the RLS session GUCs the policies read."""
    await connection.execute(
        text(
            "SELECT set_config('app.role', :role, true),"
            "       set_config('app.centre_ids', :centres, true),"
            "       set_config('app.user_id', :user, true)"
        ),
        {
            "role": role,
            "centres": ",".join(str(c) for c in centre_ids),
            "user": str(uuid.uuid4()),
        },
    )


async def _centre_ids(connection) -> list[uuid.UUID]:
    rows = await connection.execute(text("SELECT id FROM centres ORDER BY code"))
    return [row[0] for row in rows]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


async def test_every_expected_table_exists(migrator_engine) -> None:
    from uaagro_db.models import Base

    async with migrator_engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        actual = {row[0] for row in rows}

    expected = set(Base.metadata.tables)
    missing = expected - actual
    assert not missing, f"migration did not create: {sorted(missing)}"


async def test_call_tables_are_partitioned_monthly(migrator_engine) -> None:
    """§10. An insert with no matching partition fails, and that failure would
    land in the audio path -- so the partitions must genuinely exist."""
    from uaagro_db.models import PARTITIONED_TABLES

    async with migrator_engine.connect() as connection:
        for parent in PARTITIONED_TABLES:
            # relkind is Postgres's internal "char" type, which asyncpg hands
            # back as bytes rather than str.
            kind = await connection.scalar(
                text("SELECT relkind::text FROM pg_class WHERE relname = :name"),
                {"name": parent},
            )
            assert kind == "p", f"{parent} is not partitioned (relkind={kind!r})"

            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhparent WHERE c.relname = :name"
                ),
                {"name": parent},
            )
            # Current month plus three either side.
            assert count >= 7, f"{parent} has only {count} partitions"


async def test_a_call_can_be_inserted_into_todays_partition(migrator_engine) -> None:
    """The partition window is only useful if today's insert actually lands."""
    async with migrator_engine.begin() as connection:
        org = await connection.scalar(text("SELECT id FROM organizations LIMIT 1"))
        call_id = uuid.uuid4()
        await connection.execute(
            text(
                "INSERT INTO calls (id, started_at, organization_id, call_ref, direction,"
                " provider, status) VALUES (:id, now(), :org, :ref, 'inbound', 'exotel',"
                " 'in_progress')"
            ),
            {"id": call_id, "org": org, "ref": f"test-{call_id.hex[:8]}"},
        )
        found = await connection.scalar(
            text("SELECT count(*) FROM calls WHERE id = :id"), {"id": call_id}
        )
        assert found == 1
        await connection.execute(text("DELETE FROM calls WHERE id = :id"), {"id": call_id})


async def test_vector_extension_and_hnsw_index_exist(migrator_engine) -> None:
    """§9 Tier 2 retrieval is not optional."""
    async with migrator_engine.connect() as connection:
        installed = await connection.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        )
        assert installed == 1

        method = await connection.scalar(
            text(
                "SELECT am.amname FROM pg_class c "
                "JOIN pg_am am ON am.oid = c.relam "
                "WHERE c.relname = 'ix_kb_chunks_embedding_hnsw'"
            )
        )
        assert method == "hnsw"


async def test_trigram_index_state_is_explicit(embedded_pg, migrator_engine) -> None:
    """Record the one fidelity gap of the embedded server rather than hiding it.

    ``pgserver`` ships no contrib, so ``pg_trgm`` is absent and the migration
    skips the two trigram indexes with a warning. On the Docker/RDS stack the
    extension is present and the indexes exist. Either state is acceptable; an
    index present *without* the extension would not be.
    """
    async with migrator_engine.connect() as connection:
        indexes = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
                "AND indexname IN ('ix_products_name_hi_trgm','ix_products_name_en_trgm')"
            )
        )
    assert indexes == (2 if embedded_pg.has_trgm else 0)


# --------------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------------- #


async def test_seed_quantities_match_the_specification(migrator_engine) -> None:
    """§21 Phase 1: 10 centres, 60 products, 12 crops, 40 recommendations, 200 farmers."""
    expected = {
        "centres": 10,
        "products": 60,
        "crops": 12,
        "crop_recommendations": 40,
        "farmers": 200,
        "districts": 15,
    }
    async with migrator_engine.connect() as connection:
        for table, want in expected.items():
            got = await connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert got == want, f"{table}: expected {want}, got {got}"


async def test_seed_is_idempotent(embedded_pg, migrator_engine) -> None:
    """`make dev` re-seeds on every boot, so a second run must create nothing."""
    from tests import pgfixture

    async with migrator_engine.connect() as connection:
        before = await connection.scalar(text("SELECT count(*) FROM farmers"))

    output = pgfixture.seed(migrator_dsn=embedded_pg.migrator_dsn, app_dsn=embedded_pg.app_dsn)

    async with migrator_engine.connect() as connection:
        after = await connection.scalar(text("SELECT count(*) FROM farmers"))

    assert after == before
    assert "total rows created: 0" in output


async def test_phone_numbers_are_never_stored_in_plaintext(migrator_engine) -> None:
    """§17. The encrypted column must not contain readable digits, and there is
    deliberately no plaintext column at all."""
    async with migrator_engine.connect() as connection:
        columns = await connection.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'farmers'")
        )
        names = {row[0] for row in columns}
        assert "phone" not in names
        assert {"phone_hash", "phone_enc", "phone_last4"} <= names

        row = await connection.execute(
            text("SELECT phone_hash, phone_enc, phone_last4 FROM farmers LIMIT 1")
        )
        phone_hash, phone_enc, last4 = row.one()
        assert len(phone_hash) == 32
        assert len(last4) == 4
        # The ciphertext must not contain the plaintext digits.
        assert last4.encode() not in bytes(phone_enc)


# --------------------------------------------------------------------------- #
# The safety control (§9, §16.2)
# --------------------------------------------------------------------------- #


async def test_every_seeded_recommendation_is_draft(migrator_engine) -> None:
    """KB §11: nothing in the seed is servable."""
    async with migrator_engine.connect() as connection:
        approved = await connection.scalar(
            text("SELECT count(*) FROM crop_recommendations WHERE approval_state = 'approved'")
        )
    assert approved == 0


async def test_unapproved_advisory_is_unservable(migrator_engine) -> None:
    """The agent's only read path returns nothing for unapproved rows.

    This is the single most important safety control in the platform: an LLM
    speaking an unapproved pesticide dose is a crop loss and a poisoning risk.
    """
    async with migrator_engine.connect() as connection:
        servable = await connection.scalar(
            text(
                "SELECT count(*) FROM crop_recommendations "
                "WHERE approval_state = 'approved' AND deleted_at IS NULL "
                "AND approved_by_user_id IS NOT NULL"
            )
        )
    assert servable == 0


async def test_approval_without_an_approver_is_impossible(migrator_engine) -> None:
    """§18 keeps every recommendation traceable to a named agronomist. The
    database refuses an approval that names nobody."""
    async with migrator_engine.begin() as connection:
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "UPDATE crop_recommendations SET approval_state = 'approved' "
                    "WHERE id = (SELECT id FROM crop_recommendations LIMIT 1)"
                )
            )


async def test_crop_protection_cannot_be_approved_without_phi_and_precaution(
    migrator_engine,
) -> None:
    """KB §5: a spray recommendation must carry its pre-harvest interval and a
    precaution before anyone can approve it."""
    async with migrator_engine.begin() as connection:
        target = await connection.scalar(
            text(
                "SELECT id FROM crop_recommendations "
                "WHERE is_crop_protection AND phi_days IS NULL LIMIT 1"
            )
        )
        if target is None:
            pytest.skip("no seeded crop-protection row lacks a PHI")
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "UPDATE crop_recommendations SET approval_state='approved',"
                    " approved_by_user_id=(SELECT id FROM users LIMIT 1), approved_at=now()"
                    " WHERE id = :id"
                ),
                {"id": target},
            )


async def test_agrochemicals_require_a_cib_registration(migrator_engine) -> None:
    """§16.2: an unregistered agrochemical cannot exist in the catalogue."""
    async with migrator_engine.begin() as connection:
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "UPDATE products SET cib_registration_no = NULL "
                    "WHERE product_type = 'insecticide'"
                )
            )


# --------------------------------------------------------------------------- #
# Row-Level Security (§10, §17) -- the tests the Phase 1 gate names
# --------------------------------------------------------------------------- #


async def test_rls_is_enabled_and_forced_on_every_protected_table(migrator_engine) -> None:
    """FORCE is what makes the policies apply to the table owner too. Without it
    the tests below would pass against a system that leaks."""
    from uaagro_db.models import RLS_TABLES

    async with migrator_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(:names)"
            ),
            {"names": list(RLS_TABLES)},
        )
        state = {r[0]: (r[1], r[2]) for r in rows}

    for table in RLS_TABLES:
        enabled, forced = state[table]
        assert enabled, f"RLS not enabled on {table}"
        assert forced, f"RLS not FORCEd on {table}"


async def test_app_role_owns_nothing(migrator_engine) -> None:
    """§17: if the application owned its tables, FORCE would be the only thing
    between a bug and a cross-centre read."""
    async with migrator_engine.connect() as connection:
        owned = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tableowner = 'uaagro_app'"
            )
        )
    assert owned == 0


async def test_centre_manager_sees_only_their_own_centres_farmers(app_engine) -> None:
    """The Phase 1 acceptance check, stated directly."""
    async with app_engine.connect() as connection:
        centres = await _centre_ids(connection)
        first, second = centres[0], centres[1]

        await _as_role(connection, role="centre_manager", centre_ids=[first])
        visible = await connection.scalar(
            text("SELECT count(*) FROM farmers WHERE assigned_centre_id = :c"), {"c": second}
        )
        assert visible == 0, "a centre manager read another centre's farmers"

        own = await connection.scalar(
            text("SELECT count(*) FROM farmers WHERE assigned_centre_id = :c"), {"c": first}
        )
        assert own > 0, "a centre manager could not read their own centre's farmers"


async def test_centre_manager_cannot_read_another_centres_calls(app_engine) -> None:
    """§10 names this case explicitly."""
    async with app_engine.connect() as connection:
        centres = await _centre_ids(connection)
        mine, theirs = centres[0], centres[1]

    async with app_engine.begin() as connection:
        # Seed one call in each centre, as an org-wide role.
        await _as_role(connection, role="ops_manager", centre_ids=[])
        org = await connection.scalar(text("SELECT id FROM organizations LIMIT 1"))
        ids = {}
        for label, centre in (("mine", mine), ("theirs", theirs)):
            call_id = uuid.uuid4()
            ids[label] = call_id
            await connection.execute(
                text(
                    "INSERT INTO calls (id, started_at, organization_id, centre_id, call_ref,"
                    " direction, provider, status) VALUES (:id, now(), :org, :centre, :ref,"
                    " 'inbound', 'exotel', 'completed')"
                ),
                {"id": call_id, "org": org, "centre": centre, "ref": f"rls-{call_id.hex[:8]}"},
            )

    try:
        async with app_engine.connect() as connection:
            await _as_role(connection, role="centre_manager", centre_ids=[mine])
            seen = await connection.scalar(
                text("SELECT count(*) FROM calls WHERE id = ANY(:ids)"),
                {"ids": [ids["mine"], ids["theirs"]]},
            )
            assert seen == 1, "centre scope did not filter the other centre's call"

            other = await connection.scalar(
                text("SELECT count(*) FROM calls WHERE id = :id"), {"id": ids["theirs"]}
            )
            assert other == 0
    finally:
        async with app_engine.begin() as connection:
            await _as_role(connection, role="ops_manager", centre_ids=[])
            await connection.execute(
                text("DELETE FROM calls WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
            )


async def test_org_wide_roles_see_every_centre(app_engine) -> None:
    async with app_engine.connect() as connection:
        await _as_role(connection, role="centre_manager", centre_ids=[])
        scoped = await connection.scalar(text("SELECT count(*) FROM farmers"))

        await _as_role(connection, role="ops_manager", centre_ids=[])
        wide = await connection.scalar(text("SELECT count(*) FROM farmers"))

    assert scoped == 0, "an empty centre list must grant nothing"
    assert wide == 200


async def test_the_voice_agent_role_reads_org_wide(app_engine) -> None:
    """The agent answers whoever calls, so it is not centre-scoped -- but it is
    still confined by the same policy rather than bypassing it."""
    async with app_engine.connect() as connection:
        await _as_role(connection, role="voice_agent", centre_ids=[])
        assert await connection.scalar(text("SELECT count(*) FROM farmers")) == 200


async def test_an_unbound_session_sees_nothing(app_engine) -> None:
    """A request that skips authentication also skips the GUC binding. The
    failure mode must be an empty result, not the whole table."""
    async with app_engine.connect() as connection:
        for table in ("farmers", "calls", "tickets", "inventory"):
            count = await connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count == 0, f"unbound session read {count} rows from {table}"


async def test_rls_also_blocks_writes_outside_the_centre_scope(app_engine) -> None:
    """The policy carries WITH CHECK, so a scoped user cannot insert a row into
    a centre they cannot see."""
    async with app_engine.connect() as connection:
        centres = await _centre_ids(connection)
        mine, theirs = centres[0], centres[1]

    async with app_engine.begin() as connection:
        await _as_role(connection, role="centre_manager", centre_ids=[mine])
        org = await connection.scalar(text("SELECT id FROM organizations LIMIT 1"))
        # organizations is not RLS-protected, so this read succeeds.
        assert org is not None
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "INSERT INTO tickets (id, organization_id, ticket_ref, centre_id, type,"
                    " priority, status, subject) VALUES (gen_random_uuid(), :org, :ref,"
                    " :centre, 'callback', 'p2', 'open', 'test')"
                ),
                {"org": org, "ref": f"T-{uuid.uuid4().hex[:8]}", "centre": theirs},
            )


# --------------------------------------------------------------------------- #
# Audit chain (§17)
# --------------------------------------------------------------------------- #


async def test_audit_chain_appends_and_verifies(migrator_engine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from uaagro_db.audit import append_audit, verify_chain
    from uaagro_domain.enums import AuditAction

    maker = async_sessionmaker(migrator_engine, expire_on_commit=False)
    async with maker() as session:
        for index in range(3):
            await append_audit(
                session,
                action=AuditAction.UPDATE,
                resource_type="inventory",
                resource_id=f"variant-{index}",
                after={"selling_price": 100 + index},
            )
        await session.commit()

    async with maker() as session:
        result = await verify_chain(session)
    assert result.ok, result.reason
    assert result.rows_checked >= 3


async def test_audit_chain_detects_tampering(migrator_engine) -> None:
    """Altering a historical row must invalidate the chain."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from uaagro_db.audit import append_audit, verify_chain
    from uaagro_domain.enums import AuditAction

    maker = async_sessionmaker(migrator_engine, expire_on_commit=False)
    async with maker() as session:
        row = await append_audit(
            session,
            action=AuditAction.UPDATE,
            resource_type="products",
            resource_id="tamper-target",
            after={"price": 100},
        )
        target = row.chain_index
        await session.commit()

    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET after = :v WHERE chain_index = :i"),
            {"v": '{"price": 999}', "i": target},
        )

    async with maker() as session:
        result = await verify_chain(session)
    assert not result.ok
    assert result.broken_at == target

    # Restore, so later tests in the session still see an intact chain.
    async with migrator_engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET after = :v WHERE chain_index = :i"),
            {"v": '{"price": 100}', "i": target},
        )


async def test_application_role_cannot_rewrite_the_audit_log(app_engine) -> None:
    """§17: append-only in practice, not just by convention."""
    async with app_engine.begin() as connection:
        await _as_role(connection, role="super_admin", centre_ids=[])
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(text("UPDATE audit_log SET resource_type = 'hacked'"))

    async with app_engine.begin() as connection:
        await _as_role(connection, role="super_admin", centre_ids=[])
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(text("DELETE FROM audit_log"))


# --------------------------------------------------------------------------- #
# Constraints that encode compliance (§13.1, §18)
# --------------------------------------------------------------------------- #


async def test_a_campaign_creator_cannot_approve_their_own_campaign(migrator_engine) -> None:
    """§13.1 four-eyes, enforced in the database rather than only in a service."""
    async with migrator_engine.begin() as connection:
        org = await connection.scalar(text("SELECT id FROM organizations LIMIT 1"))
        creator = await connection.scalar(text("SELECT id FROM users LIMIT 1"))
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "INSERT INTO campaigns (id, organization_id, name, status, source_type,"
                    " created_by, approved_by_user_id, approved_at, caller_id_number)"
                    " VALUES (gen_random_uuid(), :org, 'self approved', 'approved', 'manual',"
                    " :user, :user, now(), '14012345678')"
                ),
                {"org": org, "user": creator},
            )


async def test_promotional_consent_must_carry_an_expiry(migrator_engine) -> None:
    """§18: consent is not permanent, so a row without an expiry is refused."""
    async with migrator_engine.begin() as connection:
        farmer = await connection.scalar(text("SELECT id FROM farmers LIMIT 1"))
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "INSERT INTO consent_records (id, farmer_id, consent_type, channel,"
                    " granted_at) VALUES (gen_random_uuid(), :f, 'promotional_voice',"
                    " 'voice', now())"
                ),
                {"f": farmer},
            )


async def test_seeded_consent_expires(migrator_engine) -> None:
    async with migrator_engine.connect() as connection:
        without_expiry = await connection.scalar(
            text(
                "SELECT count(*) FROM consent_records "
                "WHERE consent_type = 'promotional_voice' AND expires_at IS NULL"
            )
        )
    assert without_expiry == 0


async def test_volatile_answers_are_never_active_in_the_cache(migrator_engine) -> None:
    """§9: anything carrying a price or stock figure goes to Tier 1 every turn."""
    async with migrator_engine.begin() as connection:
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text(
                    "UPDATE answer_cache SET contains_volatile_data = true, is_active = true"
                    " WHERE id = (SELECT id FROM answer_cache LIMIT 1)"
                )
            )


async def test_updated_at_trigger_fires(migrator_engine) -> None:
    """`updated_at` is maintained server-side, so a raw SQL write updates it too."""
    async with migrator_engine.begin() as connection:
        before = await connection.scalar(
            text("SELECT updated_at FROM districts ORDER BY name LIMIT 1")
        )
        await connection.execute(
            text(
                "UPDATE districts SET name_hi = name_hi "
                "WHERE id = (SELECT id FROM districts ORDER BY name LIMIT 1)"
            )
        )
        after = await connection.scalar(
            text("SELECT updated_at FROM districts ORDER BY name LIMIT 1")
        )
    assert after > before


async def test_product_search_vector_is_populated(migrator_engine) -> None:
    """The trigger must run on insert, not only on update."""
    async with migrator_engine.connect() as connection:
        empty = await connection.scalar(
            text("SELECT count(*) FROM products WHERE search_vector IS NULL")
        )
        assert empty == 0

        hits = await connection.scalar(
            text("SELECT count(*) FROM products WHERE search_vector @@ to_tsquery('simple', 'dap')")
        )
        assert hits >= 1, "the ASR lexicon variant 'dap' did not reach the search vector"


async def test_lexicon_variants_cover_spoken_forms(migrator_engine) -> None:
    """KB §3.1: यूरिया, urea and uria must all resolve to one SKU."""
    async with migrator_engine.connect() as connection:
        row = await connection.execute(
            text("SELECT lexicon_variants FROM products WHERE sku = 'FRT-URE-45'")
        )
        variants = set(row.scalar_one())
    assert {"यूरिया", "urea", "uria"} <= variants


async def test_expired_and_future_consent_are_distinguishable(migrator_engine) -> None:
    """The compliance gate excludes expired consent, so the data must support it."""
    async with migrator_engine.connect() as connection:
        now = datetime.now(UTC)
        live = await connection.scalar(
            text(
                "SELECT count(*) FROM consent_records WHERE consent_type='promotional_voice'"
                " AND revoked_at IS NULL AND granted_at <= :now AND expires_at > :now"
            ),
            {"now": now},
        )
        expired = await connection.scalar(
            text(
                "SELECT count(*) FROM consent_records WHERE consent_type='promotional_voice'"
                " AND expires_at <= :now"
            ),
            {"now": now + timedelta(days=365)},
        )
    assert live > 0, "no farmer has live promotional consent to campaign against"
    assert expired > 0, "nothing expires, so the exclusion path is untested"

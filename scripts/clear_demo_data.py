"""Empty the panel of everything that did not come from a real call.

A pilot starts on a database that has been demonstrated on. The catalogue,
the centres, the crops and the staff accounts are reference data the agent
needs in order to answer at all, and they stay. Everything that represents an
event -- a call, a campaign, a contact, a follow-up, a message -- is either a
real thing that happened or a rehearsal, and a panel that mixes the two is a
panel nobody can read a number off.

Seeded farmers are cleared with the calls. A farmer row exists because
somebody rang or was rung; two hundred invented ones make "Farmers: 204" on
the Data page a lie the day the first real call lands.

Refuses to run against staging or production. This is a development and
pilot-preparation tool: on a live deployment, deleting call records is a
retention decision with a schedule (docs/RUNBOOK.md), not a script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from uaagro_db.engine import incall_session
from uaagro_domain.settings import get_settings

#: In delete order: children before parents.
#:
#: `calls` is partitioned, so its partitions empty with it. `kb_documents`
#: cascades to `kb_chunks` through the relationship, but the delete here is
#: SQL, so the chunks are named first.
EVENT_TABLES: tuple[tuple[str, str], ...] = (
    ("call_turns", "every turn of every call"),
    ("call_events", "keypresses, hand-overs, hang-ups"),
    ("dtmf_events", "keypresses"),
    ("calls", "the calls themselves"),
    ("campaign_contacts", "who a campaign was going to ring"),
    ("campaigns", "the campaigns"),
    ("tickets", "follow-ups a call opened"),
    ("whatsapp_messages", "messages a call sent"),
    ("consent_records", "consent recorded for seeded farmers"),
    ("dnd_status", "the scrub results for seeded farmers"),
    ("farmers", "the two hundred invented farmers"),
)

#: Left alone: the agent cannot answer without them.
KEPT = (
    "organizations, centres, users",
    "products, product_variants, inventory, brands",
    "crops, crop_problems, crop_recommendations",
    "agent_configs (the published scripts)",
    "kb_documents (use --knowledge to clear these too)",
)


async def clear(*, knowledge: bool, dry_run: bool) -> int:
    settings = get_settings()
    if settings.app_env in ("staging", "production"):
        sys.stderr.write(
            f"Refusing to run: APP_ENV is {settings.app_env}. On a live deployment,\n"
            "removing call records is a retention decision -- see docs/RUNBOOK.md.\n"
        )
        return 2

    tables = list(EVENT_TABLES)
    if knowledge:
        tables = [("kb_chunks", "indexed pieces"), ("kb_documents", "uploaded documents"), *tables]

    async with incall_session() as session:
        # The seed loader and the panel both write as an ops manager; the row
        # policies are written for that role, and a delete under any other one
        # silently removes nothing.
        await session.execute(text("SELECT set_config('app.role','ops_manager',true)"))
        total = 0
        for table, what in tables:
            count = int(await session.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)
            total += count
            if count == 0:
                continue
            print(f"{count:>6}  {table:<20} {what}")
            if not dry_run:
                await session.execute(text(f'DELETE FROM "{table}"'))
        if dry_run:
            print(f"\n{total} rows would be deleted. Run without --dry-run to do it.")
            return 0
        await session.commit()

    print(f"\n{total} rows deleted. Kept:")
    for line in KEPT:
        print(f"  · {line}")
    print("\nThe panel now shows only what actually happens from here.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge",
        action="store_true",
        help="also delete uploaded documents and their indexed pieces",
    )
    parser.add_argument("--dry-run", action="store_true", help="count without deleting")
    args = parser.parse_args()
    return asyncio.run(clear(knowledge=args.knowledge, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())

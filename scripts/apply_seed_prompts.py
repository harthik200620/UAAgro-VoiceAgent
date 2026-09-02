"""Copy the seed prompts onto the published agent configs.

The worker speaks whatever is *published* in ``agent_configs`` (§1 N6), and
the seed loader deliberately never touches a row that already exists -- so a
change to ``uaagro_db/seeds/prompts.py`` reaches a fresh install and nobody
else. This closes that gap for a deployment that wants the current seed
wording: it overwrites the system prompt, greeting and closing of the published
inbound and outbound rows in place, records the change in the row's changelog,
and prints what it did.

In place rather than as a new version, because this is the operator saying
"use the shipped wording" -- the panel's versioning is for edits the operator
makes and may want to roll back. Anything they had typed into those three
fields is replaced, which is why it asks for ``--yes``.

    uv run python scripts/apply_seed_prompts.py --yes

Refuses in production without ``--force``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from uaagro_db.engine import incall_session
from uaagro_db.models import AgentConfig
from uaagro_db.seeds import prompts
from uaagro_domain.enums import FlowType
from uaagro_domain.settings import get_settings

WORDING = {
    FlowType.INBOUND: (
        prompts.INBOUND_SYSTEM_PROMPT,
        prompts.INBOUND_GREETING,
        prompts.INBOUND_CLOSING,
    ),
    FlowType.OUTBOUND: (
        prompts.OUTBOUND_SYSTEM_PROMPT,
        prompts.OUTBOUND_DISCLOSURE,
        prompts.OUTBOUND_CLOSING,
    ),
}


async def apply(*, dry_run: bool) -> int:
    changed = 0
    async with incall_session() as db:
        rows = (
            await db.execute(
                select(AgentConfig).where(
                    AgentConfig.is_published.is_(True), AgentConfig.deleted_at.is_(None)
                )
            )
        ).scalars()
        for row in rows:
            wording = WORDING.get(FlowType(row.flow_type))
            if wording is None:
                continue
            system_prompt, greeting, closing = wording
            same = (
                row.system_prompt == system_prompt
                and row.greeting_template == greeting
                and row.closing_template == closing
            )
            flow = getattr(row.flow_type, "value", row.flow_type)
            label = f"{flow} v{row.version}"
            if same:
                print(f"{label}: already the seed wording")
                continue
            print(f"{label}: {'would update' if dry_run else 'updating'} prompt, greeting, closing")
            changed += 1
            if dry_run:
                continue
            row.system_prompt = system_prompt
            row.greeting_template = greeting
            row.closing_template = closing
            guardrails = dict(row.guardrails or {})
            guardrails.update(
                {
                    "max_answer_words": prompts.GUARDRAILS["max_answer_words"],
                    "name_once_per_call": True,
                    "sir_per_call": prompts.GUARDRAILS["sir_per_call"],
                }
            )
            row.guardrails = guardrails
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            row.changelog = f"{row.changelog or ''}\n{stamp}: seed prompts applied.".strip()
        if not dry_run:
            await db.commit()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="write; without it, only report")
    parser.add_argument("--force", action="store_true", help="allow in production")
    args = parser.parse_args()

    if get_settings().app_env == "production" and not args.force:
        print("refusing in production without --force", file=sys.stderr)
        return 2
    changed = asyncio.run(apply(dry_run=not args.yes))
    if not args.yes and changed:
        print(f"{changed} row(s) differ; re-run with --yes to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

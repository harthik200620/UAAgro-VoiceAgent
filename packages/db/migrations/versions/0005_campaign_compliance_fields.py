"""Make a campaign's promotional status and DLT entity explicit.

Revision ID: 0005
Revises: 0004
Created: Phase 6

Two columns §13.1's gate needs and §10 did not define.

``is_promotional`` was previously going to be inferred from whether an offer was
attached. That is the kind of inference that reads as reasonable and fails
quietly: every promotional check -- the 140-series CLI rule, the consent
requirement, the DND scrub -- turns on this one boolean, so an operator who
forgot to link the offer would get a campaign that skipped all three and dialled
from a plain mobile. Every call would connect, and the violation would be
visible only to a regulator.

Defaulting to ``true`` is deliberate. An existing row whose status nobody
recorded is treated as promotional, which subjects it to the *stricter* rules.
The opposite default would silently exempt every campaign created before this
migration from the consent check.

``dlt_entity_id`` sits alongside the template id it belongs with; §13.1 requires
both to be present before a promotional campaign can be approved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "is_promotional",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "campaigns", sa.Column("dlt_entity_id", sa.String(length=80), nullable=True)
    )

    # §13.1: a promotional campaign cannot reach an approved state without both
    # DLT identifiers. Enforced in the database as well as in the gate, because
    # the gate is code that can be bypassed by an insert and this is not.
    op.create_check_constraint(
        "promotional_needs_dlt_registration",
        "campaigns",
        "status IN ('draft','pending_approval','cancelled') "
        "OR is_promotional IS FALSE "
        "OR (dlt_entity_id IS NOT NULL AND dlt_template_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("promotional_needs_dlt_registration", "campaigns", type_="check")
    op.drop_column("campaigns", "dlt_entity_id")
    op.drop_column("campaigns", "is_promotional")

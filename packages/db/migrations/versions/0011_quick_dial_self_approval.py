"""A campaign may record that its creator approved it.

Revision ID: 0011
Revises: 0010
Created: the panel's "call these numbers now"

§13.1's four-eyes rule is a CHECK constraint: the approver must differ from
the creator. Two paths need past it, and both already existed in the service
layer without the schema allowing them: the owner's account approving its own
campaign (a business with one operator has no second pair of eyes) and the
panel's quick dial, where a development deployment waives the rule so a demo
dials in one click. Both raised an integrity error at commit.

Rather than loosen the constraint, the waiver becomes a column. A campaign
whose approver is its creator must say ``self_approved``; the constraint
still refuses the silent case, and an auditor reading the row sees the
waiver without consulting the service's configuration. Production refuses to
start with the quick-dial waiver on (``Settings.verify_production_readiness``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("self_approved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # IF EXISTS: on a fresh database revision 0001 builds `campaigns` from the
    # model, whose constraint already names `self_approved` -- a column 0001
    # does not create -- so 0001 leaves the constraint out for this revision to
    # add. On a migrated database the old constraint is there and is replaced.
    op.execute(
        "ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS ck_campaigns_approver_differs_from_creator"
    )
    op.create_check_constraint(
        "approver_differs_from_creator",
        "campaigns",
        "approved_by_user_id IS NULL OR approved_by_user_id <> created_by OR self_approved",
    )


def downgrade() -> None:
    op.drop_constraint("approver_differs_from_creator", "campaigns", type_="check")
    # A self-approved row cannot satisfy the stricter rule; its approval is
    # withdrawn rather than the downgrade refused, and the campaign returns to
    # the approval queue where a second person can look at it.
    op.execute(
        "UPDATE campaigns SET approved_by_user_id = NULL, approved_at = NULL, "
        "status = 'pending_approval' WHERE self_approved"
    )
    op.create_check_constraint(
        "approver_differs_from_creator",
        "campaigns",
        "approved_by_user_id IS NULL OR approved_by_user_id <> created_by",
    )
    op.drop_column("campaigns", "self_approved")

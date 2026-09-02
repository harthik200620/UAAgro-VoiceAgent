"""The application-level roles the RLS policies read (§10, §17).

Their own module, with no imports, because two things need them and neither can
import the other: ``rls`` builds the policy SQL from them and pulls in the whole
model metadata to do it, while ``engine`` binds them per session and has to stay
importable before the models load -- Alembic constructs an engine first.

Holding a second copy in ``engine`` was the alternative, and a duplicated
security constant is the kind that gets updated in one place.
"""

from __future__ import annotations

#: The database role the API, voice worker and background worker connect as.
#: Owns nothing, so ``FORCE ROW LEVEL SECURITY`` genuinely applies to it.
APP_ROLE = "uaagro_app"

#: Application roles that see every centre in their organisation.
ORG_WIDE_ROLES = ("super_admin", "ops_manager", "auditor")

#: The media path binds this. The agent must be able to answer any caller, so
#: it reads org-wide -- but it is still confined to one organisation.
AGENT_ROLE = "voice_agent"

#: Background jobs bind this: the post-call pipeline, the dialer, retention.
#:
#: They need one because with no ``app.role`` set the policy predicate is NULL
#: and the job sees an empty database -- the correct default, and exactly the
#: wrong behaviour for a job handed the id of a call it must enrich. A distinct
#: role rather than reusing ``ops_manager`` so that "a background job read this"
#: stays distinguishable from "a person did".
SYSTEM_ROLE = "system"

#: Every role the policies treat as org-wide, in the order they appear in the
#: generated SQL. Changing this changes the policies and needs a migration.
ORG_WIDE_ALL = (*ORG_WIDE_ROLES, AGENT_ROLE, SYSTEM_ROLE)

__all__ = (
    "AGENT_ROLE",
    "APP_ROLE",
    "ORG_WIDE_ALL",
    "ORG_WIDE_ROLES",
    "SYSTEM_ROLE",
)

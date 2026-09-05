#!/usr/bin/env bash
# Every deploy after the first: pull, preflight, build, roll, verify.
#
#   infra/deploy/deploy.sh            # deploy what is checked out
#   infra/deploy/deploy.sh v1.4.0     # check out a tag first
#
# The voice worker drains live calls for up to ten minutes before it stops
# (stop_grace_period in the compose file); a deploy during the calling window
# is safe, just not instant.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose --env-file .env -f infra/docker/docker-compose.prod.yml)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env is missing"

if [[ -n "${1:-}" ]]; then
	say "Checking out $1"
	git fetch --tags --quiet
	git checkout --quiet "$1"
else
	say "Pulling the current branch"
	git pull --ff-only --quiet
fi
echo "    at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

say "Preflight over .env"
docker run --rm -v "$ROOT:/src:ro" -w /src python:3.12-slim-bookworm \
	python scripts/deploy_preflight.py --env-file .env || die "fix .env, then run again"

say "Building"
"${COMPOSE[@]}" build --pull

say "Rolling the stack (the voice worker drains first)"
"${COMPOSE[@]}" up -d --remove-orphans

say "Migrations"
"${COMPOSE[@]}" run --rm migrate uaagro-db migrate

say "Health"
VOICE_HOST="$(grep '^VOICE_HOST=' .env | cut -d= -f2- | tr -d ' "')"
PANEL_HOST="$(grep '^PANEL_HOST=' .env | cut -d= -f2- | tr -d ' "')"
for _ in $(seq 1 30); do
	voice="$(curl -sS -o /dev/null -w '%{http_code}' "https://${VOICE_HOST}/health/ready" || true)"
	panel="$(curl -sS -o /dev/null -w '%{http_code}' "https://${PANEL_HOST}/en" || true)"
	[[ "$voice" == "200" && "$panel" == "200" ]] && break
	sleep 5
done
printf '    voice %s  panel %s\n' "${voice:-none}" "${panel:-none}"
[[ "$voice" == "200" && "$panel" == "200" ]] || die "not healthy after the roll; see: ${COMPOSE[*]} logs --tail 100"

say "Pruning images the stack no longer uses"
docker image prune -f >/dev/null
echo "    done"

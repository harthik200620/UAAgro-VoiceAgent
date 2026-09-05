#!/usr/bin/env bash
# First start of the platform on a fresh Ubuntu 24.04 host.
#
#   sudo infra/deploy/bootstrap.sh
#
# Run from the repository root, with .env filled from
# infra/deploy/production.env.example. Idempotent: every step checks before it
# acts, so running it again on a half-finished host finishes the job.
#
# What it does, in order:
#   1. Docker Engine and the compose plugin from Docker's repository.
#   2. The firewall: SSH, HTTP and HTTPS in, nothing else. Caddy needs 80 and
#      443 open to the world for certificates; every other service has no
#      published port.
#   3. Unattended security updates.
#   4. The preflight over .env -- placeholders, plaintext URLs, missing controls.
#   5. Build, start, migrate, seed.
#   6. Waits for both hostnames to answer over TLS and prints what to do next.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose --env-file .env -f infra/docker/docker-compose.prod.yml)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo: the first start installs Docker and opens the firewall"
[[ -f .env ]] || die ".env is missing -- cp infra/deploy/production.env.example .env and fill it"
grep -q '^APP_ENV=production' .env || die "APP_ENV must be production in .env"

# ---------------------------------------------------------------- 1. docker
if ! command -v docker >/dev/null 2>&1; then
	say "Installing Docker Engine"
	apt-get update -qq
	apt-get install -y -qq ca-certificates curl gnupg
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	. /etc/os-release
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
		>/etc/apt/sources.list.d/docker.list
	apt-get update -qq
	apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
	systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
if [[ -n "${SUDO_USER:-}" ]] && ! id -nG "$SUDO_USER" | grep -qw docker; then
	usermod -aG docker "$SUDO_USER"
	say "Added $SUDO_USER to the docker group (takes effect at next login)"
fi

# ---------------------------------------------------------------- 2. firewall
say "Firewall: SSH, HTTP and HTTPS only"
apt-get install -y -qq ufw >/dev/null
ufw --force default deny incoming >/dev/null
ufw --force default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null # HTTP/3
ufw --force enable >/dev/null
ufw status | sed 's/^/    /'

# ---------------------------------------------------------------- 3. updates
say "Unattended security updates"
apt-get install -y -qq unattended-upgrades >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 4. preflight
say "Preflight over .env"
docker run --rm -v "$ROOT:/src:ro" -w /src python:3.12-slim-bookworm \
	python scripts/deploy_preflight.py --env-file .env --check-dns || die "fix .env, then run again"

# ---------------------------------------------------------------- 5. the stack
say "Building the images"
"${COMPOSE[@]}" build
say "Starting the stack"
"${COMPOSE[@]}" up -d --remove-orphans
say "Waiting for the migration to finish"
until [[ "$("${COMPOSE[@]}" ps --status exited --format '{{.Name}}' | grep -c migrate || true)" -ge 1 ]]; do
	sleep 2
done
if [[ -z "$("${COMPOSE[@]}" exec -T postgres psql -U uaagro -d uaagro -tAc 'select 1 from organizations limit 1' 2>/dev/null)" ]]; then
	say "Seeding the organisation, the centres, the catalogue and the first users"
	"${COMPOSE[@]}" run --rm migrate uaagro-db seed
fi

# ---------------------------------------------------------------- 6. proof
set +e
PANEL_HOST="$(grep '^PANEL_HOST=' .env | cut -d= -f2- | tr -d ' "')"
VOICE_HOST="$(grep '^VOICE_HOST=' .env | cut -d= -f2- | tr -d ' "')"
say "Waiting for certificates and the first healthy answer"
for _ in $(seq 1 60); do
	panel="$(curl -sS -o /dev/null -w '%{http_code}' "https://${PANEL_HOST}/en" 2>/dev/null)"
	voice="$(curl -sS -o /dev/null -w '%{http_code}' "https://${VOICE_HOST}/health/ready" 2>/dev/null)"
	if [[ "$panel" == "200" && "$voice" == "200" ]]; then
		break
	fi
	sleep 5
done
printf '    panel  https://%s/en           -> HTTP %s\n' "$PANEL_HOST" "${panel:-none}"
printf '    voice  https://%s/health/ready -> HTTP %s\n' "$VOICE_HOST" "${voice:-none}"
if [[ "$panel" != "200" || "$voice" != "200" ]]; then
	echo "    Not both healthy yet. Certificates need the DNS names to point here and"
	echo "    ports 80/443 open; check: ${COMPOSE[*]} logs caddy"
	exit 1
fi

say "Up. Next:"
cat <<EOF
    1. Sign in at https://${PANEL_HOST} as admin@uaagro.in with the seed password
       (${COMPOSE[*]} logs migrate | grep password) and enrol the authenticator.
    2. Publish the inbound and outbound flows in the panel.
    3. Point the telephony provider's Voicebot at
       wss://${VOICE_HOST}/ws/voice?token=<TELEPHONY_WS_TOKEN>
    4. Make the first call. docs/VERIFICATION.md says what to check on it.
    Later deploys: infra/deploy/deploy.sh. Backups: infra/deploy/backup.sh.
EOF

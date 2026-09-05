#!/usr/bin/env bash
# Nightly database backup into the object store next to the recordings.
#
#   infra/deploy/backup.sh                 # dump, compress, upload, keep 30
#   0 2 * * * /opt/uaagro/infra/deploy/backup.sh >>/var/log/uaagro-backup.log 2>&1
#
# Recordings are already in the bucket; this adds the database. Restore is in
# docs/RUNBOOK.md and is rehearsed, not assumed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose --env-file .env -f infra/docker/docker-compose.prod.yml)
KEEP="${KEEP:-30}"

value() { grep "^$1=" .env | cut -d= -f2- | tr -d ' "'; }
BUCKET="$(value S3_BUCKET)"
ACCESS="$(value S3_ACCESS_KEY_ID)"
SECRET="$(value S3_SECRET_ACCESS_KEY)"
ENDPOINT="$(value S3_ENDPOINT)"

stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
file="uaagro-${stamp}.sql.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

"${COMPOSE[@]}" exec -T postgres pg_dump -U uaagro --no-owner uaagro | gzip -9 >"$tmp/$file"
size="$(du -h "$tmp/$file" | cut -f1)"

# mc runs in a container so the host needs no S3 client. The endpoint is the
# compose-internal MinIO address, so join the stack's network.
network="$(docker network ls --filter name=uaagro --format '{{.Name}}' | head -1)"
docker run --rm --network "$network" -v "$tmp:/backup:ro" \
	-e MC_HOST_store="${ENDPOINT/:\/\//://$ACCESS:$SECRET@}" \
	minio/mc:RELEASE.2025-04-16T18-13-26Z \
	sh -c "mc cp /backup/$file store/$BUCKET/backups/$file >/dev/null && \
	       mc ls store/$BUCKET/backups/ | sort | head -n -$KEEP | awk '{print \$NF}' | \
	       while read -r old; do [ -n \"\$old\" ] && mc rm store/$BUCKET/backups/\$old; done"

echo "$(date -u +%FT%TZ) backed up $file ($size) to $BUCKET/backups, keeping $KEEP"

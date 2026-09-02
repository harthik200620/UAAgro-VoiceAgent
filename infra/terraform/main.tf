# ap-south-1 footprint (§20).
#
# NEVER APPLIED, AND NEVER VALIDATED. There is no AWS account for this project
# and no Terraform binary on the build machine, so this has not been through
# `terraform validate`, let alone `plan`. It describes the intended shape and
# nothing about it has been checked by a machine.
#
# Run `terraform init && terraform validate` before trusting a single line of
# it. Treat every sizing figure as a starting point rather than a
# recommendation: they come from §3's stated volumes and §7's latency budget,
# not from a running system.
#
# The one thing here that is not negotiable is the region. §7 meets its budget
# by co-locating the worker, API, Postgres and Redis, and Sarvam is India-hosted.
# Moving any of them out of ap-south-1 adds a round trip to every turn and the
# budget stops being reachable by tuning.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type        = string
  default     = "ap-south-1"
  description = <<-EOT
    Mumbai. §7's budget assumes the worker, API, Postgres and Redis are
    co-located and that Sarvam is a short hop away. This is not a preference.
  EOT
}

variable "environment" {
  type    = string
  default = "production"
}

locals {
  name = "uaagro-${var.environment}"
  tags = {
    Project     = "uaagro-voice"
    Environment = var.environment
    ManagedBy   = "terraform"
    # §17: this system holds the phone numbers, locations and buying history of
    # 150,000 farmers. Tagged so a data-classification policy can find it.
    DataClass = "pii"
  }
}

# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = local.name })
}

# Three AZs. Two is the usual default and is enough for RDS multi-AZ, but a
# voice workload loses live calls when a zone goes, so the extra one buys
# capacity to absorb a zone failure rather than just data durability.
resource "aws_subnet" "private" {
  count             = 3
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = merge(local.tags, { Name = "${local.name}-private-${count.index}" })
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.name}-public-${count.index}" })
}

data "aws_availability_zones" "available" {
  state = "available"
}

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

resource "aws_db_instance" "postgres" {
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = "16"

  # §10 partitions the call tables monthly and expects them to be the largest in
  # the system by two orders of magnitude at seasonal peak (§3).
  instance_class        = "db.m6g.xlarge"
  allocated_storage     = 200
  max_allocated_storage = 2000
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  multi_az               = true
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]

  # §18's retention window is longer than this; these are operational backups,
  # not the archive. Point-in-time recovery is what the restore rehearsal in
  # docs/RUNBOOK.md exercises.
  backup_retention_period = 30
  backup_window           = "18:00-19:00" # 23:30 IST, after the calling window
  maintenance_window      = "sun:19:30-sun:20:30"

  # §17: the application connects as uaagro_app, which owns nothing. This is
  # the migrator.
  username = "uaagro"
  # Never in Terraform state in plaintext. Rotated by Secrets Manager.
  manage_master_user_password = true

  deletion_protection      = true
  delete_automated_backups = false
  skip_final_snapshot      = false
  final_snapshot_identifier = "${local.name}-final"

  performance_insights_enabled = true
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = local.tags
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-pg"
  subnet_ids = aws_subnet.private[*].id
  tags       = local.tags
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${local.name}-redis"
  description          = "Ephemeral per-call state, keyed by call_id (§4.1)"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.m6g.large"
  num_cache_clusters   = 2

  automatic_failover_enabled = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.cache.id]
  tags               = local.tags
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

# --------------------------------------------------------------------------- #
# Keys and storage
# --------------------------------------------------------------------------- #

resource "aws_kms_key" "data" {
  description = <<-EOT
    Wraps the phone-number data key and encrypts recordings (§17).

    Customer-managed rather than AWS-managed, and the difference matters: with
    SSE-S3 anyone holding s3:GetObject can read a farmer's voice. With this,
    they also need a grant on this key, which is auditable and revocable
    independently of bucket policy.
  EOT

  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_s3_bucket" "recordings" {
  bucket = "${local.name}-recordings"
  tags   = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "recordings" {
  bucket                  = aws_s3_bucket.recordings.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  rule {
    id     = "retention"
    status = "Enabled"

    filter {
      prefix = "recordings/"
    }

    # §18's window. The uploader also writes a `delete-after` date onto each
    # object, so an auditor can see the intent without reading this file --
    # this rule is what actually enforces it.
    expiration {
      days = var.recording_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}

variable "recording_retention_days" {
  type        = number
  default     = 365
  description = <<-EOT
    §18. Must match `compliance.retention_days_recordings` in
    config/defaults.yaml, or the object metadata and the lifecycle rule will
    disagree about when a recording dies -- and the metadata is what an auditor
    reads.
  EOT
}

# --------------------------------------------------------------------------- #
# Security groups
# --------------------------------------------------------------------------- #

resource "aws_security_group" "database" {
  name   = "${local.name}-db"
  vpc_id = aws_vpc.main.id

  # Only from the application tier. No bastion rule: shell access to a database
  # holding 150,000 farmers' numbers goes through Session Manager, which is
  # audited, rather than through a security-group hole that is not.
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = local.tags
}

resource "aws_security_group" "cache" {
  name   = "${local.name}-redis"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = local.tags
}

resource "aws_security_group" "app" {
  name   = "${local.name}-app"
  vpc_id = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

output "region" {
  value = var.region
}

output "recordings_bucket" {
  value = aws_s3_bucket.recordings.id
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
}

# ---------------------------------------------------------------------------
# S3: the image bytes
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "images" {
  bucket        = "${local.name_prefix}-bucket"
  force_destroy = var.use_localstack # never on a real environment
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket = aws_s3_bucket.images.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id

  # Versioning turns a delete into a tombstone, which would leave the bytes of
  # "deleted" images billable and recoverable. The metadata row is the source of
  # truth for existence, so deletes are real.
  versioning_configuration {
    status = "Suspended"
  }
}

# Skipped on LocalStack: its S3 does not report the configuration back to the
# provider's consistency check, so an apply hangs for the full timeout. The rule
# is real infrastructure on AWS, which is where incomplete uploads cost money.
resource "aws_s3_bucket_lifecycle_configuration" "images" {
  count = var.use_localstack ? 0 : 1

  bucket = aws_s3_bucket.images.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  # Browsers talk to the bucket directly in both directions - presigned POST to
  # upload, presigned GET to download - so the bucket itself has to answer the
  # preflight; the API's CORS headers do not apply here.
  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD", "POST"]
    allowed_origins = ["*"]
    expose_headers  = ["Content-Length", "Content-Type", "Content-Disposition"]
    max_age_seconds = 3000
  }
}

# ---------------------------------------------------------------------------
# DynamoDB: the image metadata
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "images" {
  name = "${local.name_prefix}-metadata"

  # On-demand: concurrent uploads from many users are exactly the bursty,
  # hard-to-forecast pattern that provisioned capacity handles badly.
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "imageId"

  attribute {
    name = "imageId"
    type = "S"
  }

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "uploadedAt"
    type = "S"
  }

  # The dominant read pattern: one user's images, newest first, optionally
  # narrowed to a date window. ISO-8601 sorts lexicographically, so the date
  # filter is a key condition rather than a post-read filter.
  #
  # Sparse by design: uploadedAt is only written once an upload is verified, so
  # pending and rejected rows never enter the index and need no filtering out.
  global_secondary_index {
    name            = "userId-uploadedAt-index"
    hash_key        = "userId"
    range_key       = "uploadedAt"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = !var.use_localstack
  }

  # Pending rows whose upload never arrives, and rejected rows once their reason
  # has been readable for a day, expire on their own. Ready rows carry no expiresAt.
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }
}

locals {
  table_arn   = aws_dynamodb_table.images.arn
  index_arn   = "${aws_dynamodb_table.images.arn}/index/*"
  objects_arn = "${aws_s3_bucket.images.arn}/*"

  # Every function, its trigger and exactly the permissions it needs. Adding an
  # endpoint means adding one entry here; the IAM role, log group, integration,
  # method and invoke permission are all derived from it. Entries without an
  # http_method are not routed through API Gateway.
  functions = {
    upload-image = {
      handler     = "src.handlers.upload_image.handler"
      description = "Register image metadata and sign a direct-to-S3 upload"
      http_method = "POST"
      resource    = "images"
      # No bytes pass through this function any more - it writes one row and
      # signs a policy locally - so it needs no more than the smallest tier.
      memory_size = 256
      timeout     = 10
      permissions = [
        { actions = ["dynamodb:PutItem"], resources = [local.table_arn] },
        # Like presigned GETs, a POST policy is only honoured if the signing
        # principal could perform the upload itself.
        { actions = ["s3:PutObject"], resources = [local.objects_arn] },
      ]
    }

    process-upload = {
      handler     = "src.handlers.process_upload.handler"
      description = "Verify uploaded objects and mark images ready or rejected"
      http_method = null
      resource    = null
      # Hashes up to max_image_bytes in 1 MiB chunks. Memory buys CPU on Lambda,
      # and SHA-256 over 20 MiB is CPU-bound.
      memory_size = 512
      timeout     = 60
      permissions = [
        { actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"], resources = [local.table_arn] },
        # GetObject also authorises HeadObject.
        { actions = ["s3:GetObject", "s3:DeleteObject"], resources = [local.objects_arn] },
      ]
    }

    list-images = {
      handler     = "src.handlers.list_images.handler"
      description = "List images with filters and pagination"
      http_method = "GET"
      resource    = "images"
      memory_size = 256
      timeout     = 15
      permissions = [
        { actions = ["dynamodb:Query"], resources = [local.table_arn, local.index_arn] },
        { actions = ["dynamodb:Scan"], resources = [local.table_arn] },
      ]
    }

    get-image = {
      handler     = "src.handlers.get_image.handler"
      description = "Fetch one image's metadata"
      http_method = "GET"
      resource    = "image"
      memory_size = 256
      timeout     = 10
      permissions = [
        { actions = ["dynamodb:GetItem"], resources = [local.table_arn] },
      ]
    }

    download-image = {
      handler     = "src.handlers.download_image.handler"
      description = "Redirect to a presigned URL for the image bytes"
      http_method = "GET"
      resource    = "image_content"
      memory_size = 256
      timeout     = 10
      permissions = [
        { actions = ["dynamodb:GetItem"], resources = [local.table_arn] },
        # Presigning is a local signing operation, but the signature is only
        # honoured if the signing principal itself may read the object.
        { actions = ["s3:GetObject"], resources = [local.objects_arn] },
      ]
    }

    delete-image = {
      handler     = "src.handlers.delete_image.handler"
      description = "Delete an image and its metadata"
      http_method = "DELETE"
      resource    = "image"
      memory_size = 256
      timeout     = 15
      permissions = [
        { actions = ["dynamodb:DeleteItem"], resources = [local.table_arn] },
        { actions = ["s3:DeleteObject"], resources = [local.objects_arn] },
      ]
    }
  }

  api_routes = { for name, fn in local.functions : name => fn if fn.http_method != null }
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# One role per function rather than one shared role: the list endpoint cannot
# delete, the download endpoint cannot write, and a bug in any single handler
# cannot reach beyond what that endpoint legitimately does.
resource "aws_iam_role" "function" {
  for_each = local.functions

  name               = "${local.name_prefix}-${each.key}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "function" {
  for_each = local.functions

  dynamic "statement" {
    for_each = each.value.permissions
    content {
      effect    = "Allow"
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.functions

  name   = "${local.name_prefix}-${each.key}-policy"
  role   = aws_iam_role.function[each.key].id
  policy = data.aws_iam_policy_document.function[each.key].json
}

# Created explicitly so retention is enforced; Lambda's implicit group never expires.
resource "aws_cloudwatch_log_group" "function" {
  for_each = local.functions

  name              = "/aws/lambda/${local.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "function" {
  for_each = local.functions

  function_name = "${local.name_prefix}-${each.key}"
  description   = each.value.description
  role          = aws_iam_role.function[each.key].arn
  handler       = each.value.handler
  runtime       = var.lambda_runtime
  memory_size   = each.value.memory_size
  timeout       = each.value.timeout

  # A single zip shared by every function: the package holds no third-party
  # code (boto3 is in the runtime), so it is small enough that splitting it per
  # function would buy nothing and multiply deploy time.
  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)

  environment {
    variables = {
      IMAGES_TABLE             = aws_dynamodb_table.images.name
      IMAGES_BUCKET            = aws_s3_bucket.images.bucket
      IMAGES_USER_INDEX        = "userId-uploadedAt-index"
      MAX_IMAGE_BYTES          = tostring(var.max_image_bytes)
      UPLOAD_URL_TTL_SECONDS   = tostring(var.upload_url_ttl_seconds)
      DOWNLOAD_URL_TTL_SECONDS = tostring(var.download_url_ttl_seconds)
      LOG_LEVEL                = var.log_level
      AWS_ENDPOINT_URL         = var.use_localstack ? var.lambda_internal_endpoint : ""
      S3_PUBLIC_ENDPOINT       = var.use_localstack ? var.s3_public_endpoint : ""
    }
  }

  depends_on = [
    aws_iam_role_policy.function,
    aws_cloudwatch_log_group.function,
  ]
}

# ---------------------------------------------------------------------------
# Upload processing: S3 ObjectCreated -> process-upload
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "s3_upload_notification" {
  statement_id   = "AllowInvokeFromS3"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.function["process-upload"].function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.images.arn
  source_account = var.use_localstack ? null : data.aws_caller_identity.current[0].account_id
}

data "aws_caller_identity" "current" {
  count = var.use_localstack ? 0 : 1
}

resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.images.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.function["process-upload"].arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "images/"
  }

  depends_on = [aws_lambda_permission.s3_upload_notification]
}

# S3 invokes asynchronously. Bounded retries cover transient S3/DynamoDB errors
# (the handler re-raises those); a pending row whose event is never processed
# expires through the table's TTL rather than lingering forever.
resource "aws_lambda_function_event_invoke_config" "process_upload" {
  function_name                = aws_lambda_function.function["process-upload"].function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

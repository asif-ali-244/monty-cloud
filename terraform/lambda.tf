locals {
  table_arn   = aws_dynamodb_table.images.arn
  index_arn   = "${aws_dynamodb_table.images.arn}/index/*"
  objects_arn = "${aws_s3_bucket.images.arn}/*"

  # Every function, its route and exactly the permissions it needs. Adding an
  # endpoint means adding one entry here; the IAM role, log group, integration,
  # method and invoke permission are all derived from it.
  functions = {
    upload-image = {
      handler     = "src.handlers.upload_image.handler"
      description = "Upload an image with its metadata"
      http_method = "POST"
      resource    = "images"
      # Base64 decoding of a 5 MB image is the memory-hungry path, and on Lambda
      # CPU scales with memory, so this is the cheapest way to keep it quick.
      memory_size = 512
      timeout     = 20
      permissions = [
        { actions = ["dynamodb:PutItem"], resources = [local.table_arn] },
        { actions = ["s3:PutObject"], resources = [local.objects_arn] },
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

  # A single zip shared by all five functions: the package holds no third-party
  # code (boto3 is in the runtime), so it is small enough that splitting it per
  # function would buy nothing and make deploys five times slower.
  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)

  environment {
    variables = {
      IMAGES_TABLE             = aws_dynamodb_table.images.name
      IMAGES_BUCKET            = aws_s3_bucket.images.bucket
      IMAGES_USER_INDEX        = "userId-uploadedAt-index"
      MAX_IMAGE_BYTES          = tostring(var.max_image_bytes)
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

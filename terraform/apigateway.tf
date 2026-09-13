resource "aws_api_gateway_rest_api" "images" {
  name        = "${local.name_prefix}-api"
  description = "Image upload and catalogue API"

  # No binary_media_types: image bytes never pass through the API. Uploads go
  # to S3 on a presigned POST and downloads redirect to a presigned GET.
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# /images
resource "aws_api_gateway_resource" "images" {
  rest_api_id = aws_api_gateway_rest_api.images.id
  parent_id   = aws_api_gateway_rest_api.images.root_resource_id
  path_part   = "images"
}

# /images/{imageId}
resource "aws_api_gateway_resource" "image" {
  rest_api_id = aws_api_gateway_rest_api.images.id
  parent_id   = aws_api_gateway_resource.images.id
  path_part   = "{imageId}"
}

# /images/{imageId}/content
resource "aws_api_gateway_resource" "image_content" {
  rest_api_id = aws_api_gateway_rest_api.images.id
  parent_id   = aws_api_gateway_resource.image.id
  path_part   = "content"
}

locals {
  api_resources = {
    images        = aws_api_gateway_resource.images.id
    image         = aws_api_gateway_resource.image.id
    image_content = aws_api_gateway_resource.image_content.id
  }
}

resource "aws_api_gateway_method" "function" {
  for_each = local.api_routes

  rest_api_id = aws_api_gateway_rest_api.images.id
  resource_id = local.api_resources[each.value.resource]
  http_method = each.value.http_method

  # Deliberately open for the assessment. Production would attach a Cognito or
  # Lambda authorizer here; the handlers already prefer the authorizer's claims
  # over the X-User-Id header, so that swap needs no application change.
  authorization = "NONE"

  request_parameters = each.value.resource == "images" ? {} : {
    "method.request.path.imageId" = true
  }
}

resource "aws_api_gateway_integration" "function" {
  for_each = local.api_routes

  rest_api_id = aws_api_gateway_rest_api.images.id
  resource_id = local.api_resources[each.value.resource]
  http_method = aws_api_gateway_method.function[each.key].http_method

  # AWS_PROXY always POSTs to the Lambda invoke endpoint, whatever the caller used.
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.function[each.key].invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  for_each = local.api_routes

  statement_id  = "AllowInvokeFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.function[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.images.execution_arn}/*/*"
}

# --- CORS preflight -------------------------------------------------------
# Handlers set CORS headers on real responses, but a browser's OPTIONS probe
# never reaches Lambda, so it is answered by a mock integration.

locals {
  cors_resources = {
    images = aws_api_gateway_resource.images.id
    image  = aws_api_gateway_resource.image.id
  }
  cors_allowed_methods = {
    images = "GET,POST,OPTIONS"
    image  = "GET,DELETE,OPTIONS"
  }
}

resource "aws_api_gateway_method" "options" {
  for_each = local.cors_resources

  rest_api_id   = aws_api_gateway_rest_api.images.id
  resource_id   = each.value
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.images.id
  resource_id = each.value
  http_method = aws_api_gateway_method.options[each.key].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = jsonencode({ statusCode = 200 })
  }
}

resource "aws_api_gateway_method_response" "options" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.images.id
  resource_id = each.value
  http_method = aws_api_gateway_method.options[each.key].http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.images.id
  resource_id = each.value
  http_method = aws_api_gateway_method.options[each.key].http_method
  status_code = aws_api_gateway_method_response.options[each.key].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-User-Id'"
    "method.response.header.Access-Control-Allow-Methods" = "'${local.cors_allowed_methods[each.key]}'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }

  depends_on = [aws_api_gateway_integration.options]
}

# --- Deployment -----------------------------------------------------------

resource "aws_api_gateway_deployment" "images" {
  rest_api_id = aws_api_gateway_rest_api.images.id

  # REST API deployments are snapshots, not live configuration: without a
  # trigger keyed to the routes, an edited method would never reach the stage.
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.images,
      aws_api_gateway_resource.image,
      aws_api_gateway_resource.image_content,
      aws_api_gateway_method.function,
      aws_api_gateway_integration.function,
      aws_api_gateway_method.options,
      aws_api_gateway_integration.options,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.function,
    aws_api_gateway_integration_response.options,
  ]
}

resource "aws_api_gateway_stage" "images" {
  rest_api_id   = aws_api_gateway_rest_api.images.id
  deployment_id = aws_api_gateway_deployment.images.id
  stage_name    = var.environment
}

resource "aws_api_gateway_method_settings" "images" {
  rest_api_id = aws_api_gateway_rest_api.images.id
  stage_name  = aws_api_gateway_stage.images.stage_name
  method_path = "*/*"

  settings {
    # A shared upper bound so one client cannot exhaust the account's Lambda
    # concurrency for everyone else. Per-client quotas would need API keys.
    throttling_rate_limit  = 200
    throttling_burst_limit = 400
    metrics_enabled        = !var.use_localstack
  }
}

output "api_base_url" {
  description = "Base URL for the deployed API."
  value       = var.use_localstack ? "${var.localstack_endpoint}/restapis/${aws_api_gateway_rest_api.images.id}/${var.environment}/_user_request_" : aws_api_gateway_stage.images.invoke_url
}

output "rest_api_id" {
  description = "API Gateway REST API id."
  value       = aws_api_gateway_rest_api.images.id
}

output "images_table" {
  description = "DynamoDB metadata table."
  value       = aws_dynamodb_table.images.name
}

output "images_bucket" {
  description = "S3 bucket holding the image objects."
  value       = aws_s3_bucket.images.bucket
}

output "function_names" {
  description = "Deployed Lambda functions, keyed by route."
  value       = { for key, fn in aws_lambda_function.function : key => fn.function_name }
}

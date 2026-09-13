variable "project_name" {
  description = "Prefix applied to every resource name."
  type        = string
  default     = "monty-images"
}

variable "environment" {
  description = "Deployment environment (local, dev, prod)."
  type        = string
  default     = "local"
}

variable "aws_region" {
  description = "Region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "use_localstack" {
  description = "Point every AWS endpoint at LocalStack and skip account checks."
  type        = bool
  default     = true
}

variable "localstack_endpoint" {
  description = "LocalStack edge endpoint, as reached from the machine running Terraform."
  type        = string
  default     = "http://localhost:4566"
}

variable "lambda_internal_endpoint" {
  description = <<-EOT
    Endpoint the Lambda containers use to reach AWS services. Inside LocalStack
    the function runs in a sibling container, so localhost is its own loopback;
    localhost.localstack.cloud resolves back to the LocalStack container.
    Empty means "use the real AWS endpoints".
  EOT
  type        = string
  default     = "http://localhost.localstack.cloud:4566"
}

variable "s3_public_endpoint" {
  description = "Endpoint used to sign upload and download URLs, as reached from the caller's browser."
  type        = string
  default     = "http://localhost:4566"
}

variable "lambda_package" {
  description = "Path to the deployment zip produced by scripts/package.sh."
  type        = string
  default     = "../build/lambda.zip"
}

variable "lambda_runtime" {
  description = "Lambda Python runtime. The brief asks for 3.7+; 3.7 is end-of-life."
  type        = string
  default     = "python3.12"
}

variable "log_retention_days" {
  description = "CloudWatch log retention for every function."
  type        = number
  default     = 14
}

variable "max_image_bytes" {
  description = "Largest accepted image. Enforced by the upload policy in S3; no API or Lambda payload limit applies."
  type        = number
  default     = 20971520
}

variable "upload_url_ttl_seconds" {
  description = "Lifetime of a presigned upload form."
  type        = number
  default     = 900
}

variable "download_url_ttl_seconds" {
  description = "Default lifetime of a presigned download URL."
  type        = number
  default     = 900
}

variable "log_level" {
  description = "Application log level."
  type        = string
  default     = "INFO"
}

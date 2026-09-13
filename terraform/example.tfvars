# Local development against LocalStack (the defaults).
# Copy to local.tfvars and pass with: terraform apply -var-file=local.tfvars
project_name = "monty-images"
environment  = "local"
aws_region   = "us-east-1"

use_localstack           = true
localstack_endpoint      = "http://localhost:4566"
lambda_internal_endpoint = "http://localhost.localstack.cloud:4566"
s3_public_endpoint       = "http://localhost:4566"

max_image_bytes          = 20971520
upload_url_ttl_seconds   = 900
download_url_ttl_seconds = 900
log_level                = "INFO"

# Deploying to a real account instead:
#   use_localstack = false
#   environment    = "dev"
# The endpoint variables are then ignored.

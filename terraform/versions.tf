terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

# One provider drives both targets. Against LocalStack every service endpoint is
# redirected at the edge port and the credential/account checks are skipped;
# against real AWS the dynamic block disappears and the provider behaves normally,
# so the same configuration deploys to both.
provider "aws" {
  region = var.aws_region

  access_key                  = var.use_localstack ? "test" : null
  secret_key                  = var.use_localstack ? "test" : null
  s3_use_path_style           = var.use_localstack
  skip_credentials_validation = var.use_localstack
  skip_metadata_api_check     = var.use_localstack
  skip_requesting_account_id  = var.use_localstack

  dynamic "endpoints" {
    for_each = var.use_localstack ? [1] : []
    content {
      apigateway = var.localstack_endpoint
      cloudwatch = var.localstack_endpoint
      dynamodb   = var.localstack_endpoint
      iam        = var.localstack_endpoint
      lambda     = var.localstack_endpoint
      logs       = var.localstack_endpoint
      s3         = var.localstack_endpoint
      sts        = var.localstack_endpoint
    }
  }

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

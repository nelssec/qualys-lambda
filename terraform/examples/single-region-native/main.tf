terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

module "qualys_scanner" {
  source = "../../modules/scanner-native"

  stack_name          = var.stack_name
  qualys_pod          = var.qualys_pod
  qualys_access_token = var.qualys_access_token
  qscanner_layer_zip  = var.qscanner_layer_zip

  enable_s3_results        = true
  enable_sns_notifications = true
  enable_scan_cache        = true
  cache_ttl_days           = 30

  scanner_memory_size      = 2048
  scanner_timeout          = 900
  scanner_ephemeral_storage = 2048

  tags = {
    Environment = "production"
    Application = "qualys-lambda-scanner"
    ManagedBy   = "Terraform"
  }
}

# Outputs
output "scanner_lambda_arn" {
  description = "ARN of the Scanner Lambda function"
  value       = module.qualys_scanner.scanner_lambda_arn
}

output "scanner_lambda_name" {
  description = "Name of the Scanner Lambda function"
  value       = module.qualys_scanner.scanner_lambda_name
}

output "qscanner_layer_arn" {
  description = "ARN of the QScanner Lambda Layer"
  value       = module.qualys_scanner.qscanner_layer_arn
}

output "qualys_secret_arn" {
  description = "ARN of the Qualys credentials secret"
  value       = module.qualys_scanner.qualys_secret_arn
}

output "scan_results_bucket" {
  description = "Name of the S3 bucket for scan results"
  value       = module.qualys_scanner.scan_results_bucket_name
}

output "scan_notifications_topic" {
  description = "ARN of the SNS topic for scan notifications"
  value       = module.qualys_scanner.scan_notifications_topic_arn
}

output "scan_cache_table" {
  description = "Name of the DynamoDB scan cache table"
  value       = module.qualys_scanner.scan_cache_table_name
}

output "cloudtrail_name" {
  description = "Name of the CloudTrail trail"
  value       = module.qualys_scanner.cloudtrail_name
}

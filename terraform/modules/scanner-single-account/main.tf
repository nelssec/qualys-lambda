terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

locals {
  stack_name = var.stack_name != "" ? var.stack_name : "qualys-lambda-scanner-${var.region}"
}

# Deploy CloudFormation stack
resource "aws_cloudformation_stack" "scanner" {
  name = local.stack_name

  template_body = file("${path.module}/../../../cloudformation/single-account.yaml")

  parameters = {
    QualysPod                 = var.qualys_pod
    QualysAccessToken         = var.qualys_access_token
    ScannerImageUri           = var.scanner_image_uri
    EnableS3Results           = var.enable_s3_results ? "true" : "false"
    EnableSNSNotifications    = var.enable_sns_notifications ? "true" : "false"
    ScannerMemorySize         = var.scanner_memory_size
    ScannerTimeout            = var.scanner_timeout
    ScannerEphemeralStorage   = var.scanner_ephemeral_storage
  }

  capabilities = ["CAPABILITY_NAMED_IAM"]

  tags = merge(
    var.tags,
    {
      ManagedBy = "Terraform"
      Module    = "qualys-lambda-scanner"
    }
  )
}

# Outputs from CloudFormation stack
output "scanner_lambda_arn" {
  description = "ARN of the Scanner Lambda function"
  value       = lookup(aws_cloudformation_stack.scanner.outputs, "ScannerLambdaArn", "")
}

output "qualys_secret_arn" {
  description = "ARN of the Qualys credentials secret"
  value       = lookup(aws_cloudformation_stack.scanner.outputs, "QualysSecretArn", "")
}

output "scan_results_bucket_name" {
  description = "Name of the S3 bucket for scan results"
  value       = var.enable_s3_results ? lookup(aws_cloudformation_stack.scanner.outputs, "ScanResultsBucketName", "") : ""
}

output "scan_notifications_topic_arn" {
  description = "ARN of the SNS topic for scan notifications"
  value       = var.enable_sns_notifications ? lookup(aws_cloudformation_stack.scanner.outputs, "ScanNotificationsTopicArn", "") : ""
}

output "cloudtrail_name" {
  description = "Name of the CloudTrail trail"
  value       = lookup(aws_cloudformation_stack.scanner.outputs, "CloudTrailName", "")
}

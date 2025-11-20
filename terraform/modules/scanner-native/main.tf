terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0"
    }
  }
}

locals {
  function_name = "${var.stack_name}-scanner"
  layer_name    = "${var.stack_name}-qscanner"
}

# Create Lambda Layer for QScanner binary
resource "aws_lambda_layer_version" "qscanner" {
  filename            = var.qscanner_layer_zip
  layer_name          = local.layer_name
  compatible_runtimes = ["python3.11", "python3.10", "python3.9"]
  description         = "QScanner binary from Qualys (${filesha256(var.qscanner_layer_zip)})"

  lifecycle {
    create_before_destroy = true
  }
}

# Package Lambda function code
data "archive_file" "lambda_function" {
  type        = "zip"
  source_file = "${path.module}/../../../scanner-lambda/lambda_function.py"
  output_path = "${path.module}/builds/lambda_function.zip"
}

# Create Secrets Manager secret for Qualys credentials
resource "aws_secretsmanager_secret" "qualys_credentials" {
  name        = "${var.stack_name}-qualys-credentials"
  description = "Qualys credentials for Lambda scanner"

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-qualys-credentials"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_secretsmanager_secret_version" "qualys_credentials" {
  secret_id = aws_secretsmanager_secret.qualys_credentials.id
  secret_string = jsonencode({
    qualys_pod          = var.qualys_pod
    qualys_access_token = var.qualys_access_token
  })
}

# DynamoDB table for scan caching
resource "aws_dynamodb_table" "scan_cache" {
  count = var.enable_scan_cache ? 1 : 0

  name         = "${var.stack_name}-scan-cache"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "function_arn"

  attribute {
    name = "function_arn"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scan-cache"
      ManagedBy = "Terraform"
    }
  )
}

# S3 bucket for scan results
resource "aws_s3_bucket" "scan_results" {
  count = var.enable_s3_results ? 1 : 0

  bucket = "${var.stack_name}-scan-results-${data.aws_caller_identity.current.account_id}"

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scan-results"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_s3_bucket_versioning" "scan_results" {
  count = var.enable_s3_results ? 1 : 0

  bucket = aws_s3_bucket.scan_results[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "scan_results" {
  count = var.enable_s3_results ? 1 : 0

  bucket = aws_s3_bucket.scan_results[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "scan_results" {
  count = var.enable_s3_results ? 1 : 0

  bucket = aws_s3_bucket.scan_results[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "scan_results" {
  count = var.enable_s3_results ? 1 : 0

  bucket = aws_s3_bucket.scan_results[0].id

  rule {
    id     = "DeleteOldScans"
    status = "Enabled"

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# SNS topic for scan notifications
resource "aws_sns_topic" "scan_notifications" {
  count = var.enable_sns_notifications ? 1 : 0

  name         = "${var.stack_name}-scan-notifications"
  display_name = "Qualys Lambda Scan Notifications"

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scan-notifications"
      ManagedBy = "Terraform"
    }
  )
}

# CloudWatch Log Group for Scanner Lambda
resource "aws_cloudwatch_log_group" "scanner_lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scanner-logs"
      ManagedBy = "Terraform"
    }
  )
}

# IAM Role for Scanner Lambda
resource "aws_iam_role" "scanner_lambda" {
  name = "${var.stack_name}-scanner-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scanner-lambda-role"
      ManagedBy = "Terraform"
    }
  )
}

# IAM Policy for Scanner Lambda
resource "aws_iam_role_policy" "scanner_lambda" {
  name = "ScannerLambdaPolicy"
  role = aws_iam_role.scanner_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ECRAuthToken"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRRepositoryAccess"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:DescribeImages"
        ]
        Resource = "arn:aws:ecr:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:repository/*"
      },
      {
        Sid    = "LambdaRead"
        Effect = "Allow"
        Action = [
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:TagResource"
        ]
        Resource = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:*"
      },
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.qualys_credentials.arn
      }
      ],
      var.enable_scan_cache ? [{
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.scan_cache[0].arn
      }] : [],
      var.enable_s3_results ? [{
        Sid    = "S3Write"
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.scan_results[0].arn}/*"
      }] : [],
      var.enable_sns_notifications ? [{
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.scan_notifications[0].arn
      }] : []
    )
  })
}

# Attach AWS managed policy for basic Lambda execution
resource "aws_iam_role_policy_attachment" "scanner_lambda_basic" {
  role       = aws_iam_role.scanner_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Scanner Lambda Function
resource "aws_lambda_function" "scanner" {
  filename         = data.archive_file.lambda_function.output_path
  function_name    = local.function_name
  role             = aws_iam_role.scanner_lambda.arn
  handler          = "lambda_function.lambda_handler"
  source_code_hash = data.archive_file.lambda_function.output_base64sha256
  runtime          = "python3.11"
  memory_size      = var.scanner_memory_size
  timeout          = var.scanner_timeout

  layers = [aws_lambda_layer_version.qscanner.arn]

  ephemeral_storage {
    size = var.scanner_ephemeral_storage
  }

  environment {
    variables = {
      QUALYS_SECRET_ARN = aws_secretsmanager_secret.qualys_credentials.arn
      RESULTS_S3_BUCKET = var.enable_s3_results ? aws_s3_bucket.scan_results[0].id : ""
      SNS_TOPIC_ARN     = var.enable_sns_notifications ? aws_sns_topic.scan_notifications[0].arn : ""
      SCAN_CACHE_TABLE  = var.enable_scan_cache ? aws_dynamodb_table.scan_cache[0].name : ""
      SCAN_TIMEOUT      = "300"
      CACHE_TTL_DAYS    = tostring(var.cache_ttl_days)
      QSCANNER_PATH     = "/opt/bin/qscanner"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.scanner_lambda,
    aws_iam_role_policy.scanner_lambda,
    aws_iam_role_policy_attachment.scanner_lambda_basic
  ]

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-scanner"
      ManagedBy = "Terraform"
    }
  )
}

# CloudTrail S3 bucket
resource "aws_s3_bucket" "cloudtrail" {
  bucket = "${var.stack_name}-cloudtrail-${data.aws_caller_identity.current.account_id}"

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-cloudtrail"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_s3_bucket_versioning" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    id     = "DeleteOldLogs"
    status = "Enabled"

    expiration {
      days = 7
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AWSCloudTrailAclCheck"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:GetBucketAcl"
        Resource = aws_s3_bucket.cloudtrail.arn
      },
      {
        Sid    = "AWSCloudTrailWrite"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.cloudtrail.arn}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl" = "bucket-owner-full-control"
          }
        }
      }
    ]
  })
}

# CloudWatch Log Group for CloudTrail
resource "aws_cloudwatch_log_group" "cloudtrail" {
  name              = "/aws/cloudtrail/${var.stack_name}"
  retention_in_days = 7

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-cloudtrail-logs"
      ManagedBy = "Terraform"
    }
  )
}

# IAM Role for CloudTrail to write to CloudWatch Logs
resource "aws_iam_role" "cloudtrail_logs" {
  name = "${var.stack_name}-cloudtrail-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-cloudtrail-logs-role"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_iam_role_policy" "cloudtrail_logs" {
  name = "CloudTrailLogsPolicy"
  role = aws_iam_role.cloudtrail_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
      }
    ]
  })
}

# CloudTrail
resource "aws_cloudtrail" "main" {
  name                          = "${var.stack_name}-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true
  cloud_watch_logs_group_arn    = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
  cloud_watch_logs_role_arn     = aws_iam_role.cloudtrail_logs.arn

  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = true
  }

  depends_on = [
    aws_s3_bucket_policy.cloudtrail
  ]

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-trail"
      ManagedBy = "Terraform"
    }
  )
}

# EventBridge Rules
resource "aws_cloudwatch_event_rule" "lambda_create" {
  name        = "${var.stack_name}-lambda-create"
  description = "Trigger scanner when Lambda function is created"

  event_pattern = jsonencode({
    source      = ["aws.lambda"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["lambda.amazonaws.com"]
      eventName   = ["CreateFunction20150331"]
    }
  })

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-lambda-create"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_cloudwatch_event_target" "lambda_create" {
  rule      = aws_cloudwatch_event_rule.lambda_create.name
  target_id = "ScannerLambdaTarget"
  arn       = aws_lambda_function.scanner.arn
}

resource "aws_lambda_permission" "lambda_create" {
  statement_id  = "AllowExecutionFromEventBridgeCreate"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scanner.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_create.arn
}

resource "aws_cloudwatch_event_rule" "lambda_update_code" {
  name        = "${var.stack_name}-lambda-update-code"
  description = "Trigger scanner when Lambda function code is updated"

  event_pattern = jsonencode({
    source      = ["aws.lambda"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["lambda.amazonaws.com"]
      eventName   = ["UpdateFunctionCode20150331v2"]
    }
  })

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-lambda-update-code"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_cloudwatch_event_target" "lambda_update_code" {
  rule      = aws_cloudwatch_event_rule.lambda_update_code.name
  target_id = "ScannerLambdaTarget"
  arn       = aws_lambda_function.scanner.arn
}

resource "aws_lambda_permission" "lambda_update_code" {
  statement_id  = "AllowExecutionFromEventBridgeUpdateCode"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scanner.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_update_code.arn
}

resource "aws_cloudwatch_event_rule" "lambda_update_config" {
  name        = "${var.stack_name}-lambda-update-config"
  description = "Trigger scanner when Lambda function configuration is updated"

  event_pattern = jsonencode({
    source      = ["aws.lambda"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["lambda.amazonaws.com"]
      eventName   = ["UpdateFunctionConfiguration20150331v2"]
    }
  })

  tags = merge(
    var.tags,
    {
      Name      = "${var.stack_name}-lambda-update-config"
      ManagedBy = "Terraform"
    }
  )
}

resource "aws_cloudwatch_event_target" "lambda_update_config" {
  rule      = aws_cloudwatch_event_rule.lambda_update_config.name
  target_id = "ScannerLambdaTarget"
  arn       = aws_lambda_function.scanner.arn
}

resource "aws_lambda_permission" "lambda_update_config" {
  statement_id  = "AllowExecutionFromEventBridgeUpdateConfig"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scanner.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_update_config.arn
}

# Data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Qualys Lambda Scanner

Automated security scanning for AWS Lambda functions using Qualys QScanner. This solution provides event-driven scanning of Lambda deployments across single or multiple AWS accounts, with support for organization-wide deployment via CloudFormation StackSets.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Deployment Models](#deployment-models)
- [Prerequisites](#prerequisites)
- [Single Account Deployment](#single-account-deployment)
- [Multi-Account StackSet Deployment](#multi-account-stackset-deployment)
- [Centralized Hub-Spoke Deployment](#centralized-hub-spoke-deployment)
- [Bulk Scanning Existing Functions](#bulk-scanning-existing-functions)
- [Qualys Image Tagging](#qualys-image-tagging)
- [Configuration Reference](#configuration-reference)
- [Security Features](#security-features)
- [Monitoring and Alerting](#monitoring-and-alerting)
- [Troubleshooting](#troubleshooting)
- [Maintenance](#maintenance)

## Overview

The Qualys Lambda Scanner automatically scans Lambda functions for vulnerabilities and secrets when they are created or updated. The solution:

- Monitors Lambda API calls via CloudTrail and EventBridge
- Triggers scans on CreateFunction, UpdateFunctionCode, and UpdateFunctionConfiguration events
- Caches scan results by code hash (CodeSha256) to prevent redundant scans
- Stores results in S3 and sends notifications via SNS
- Tags scanned functions with scan metadata for audit and compliance
- Supports both Zip-based and container-based Lambda functions

## Architecture

### Event Flow

```
Lambda API Call (Create/Update)
        |
        v
   CloudTrail
        |
        v
   EventBridge Rule
        |
        v
Scanner Lambda Function
        |
        +---> DynamoDB (cache check by CodeSha256)
        |
        +---> QScanner binary execution
        |
        +---> Qualys API (results upload)
        |
        +---> S3 (local results storage)
        |
        +---> SNS (notifications)
        |
        +---> Lambda Tags (scan metadata)
```

### Components

| Component | Purpose |
|-----------|---------|
| Scanner Lambda | Executes QScanner against target Lambda functions |
| Lambda Layer | Contains the QScanner binary at /opt/bin/qscanner |
| DynamoDB Table | Caches scan results by CodeSha256 with TTL |
| S3 Bucket | Stores scan result artifacts |
| SNS Topic | Publishes scan completion notifications |
| SQS Dead Letter Queue | Captures failed scan invocations |
| KMS Key | Encrypts DynamoDB, SQS, SNS, and CloudWatch Logs |
| Secrets Manager | Stores Qualys credentials securely |
| EventBridge Rules | Triggers scanner on Lambda events |

## Deployment Models

### Single Account

Deploy the scanner in a single AWS account. Suitable for small organizations or isolated workloads.

```
[Account]
    |
    +-- Scanner Lambda
    +-- EventBridge Rules
    +-- CloudTrail (existing or new)
```

### Multi-Account StackSet

Deploy via CloudFormation StackSet to multiple accounts. Each account receives its own scanner instance. Suitable for organizations that want distributed scanning with no cross-account dependencies.

```
[Management Account]
    |
    +-- StackSet (deploys to target OUs)
    |
    v
[Account A]          [Account B]          [Account C]
    |                    |                    |
    +-- Scanner          +-- Scanner          +-- Scanner
    +-- EventBridge      +-- EventBridge      +-- EventBridge
```

### Centralized Hub-Spoke

Deploy a single scanner in a security account (hub) that scans Lambda functions across all member accounts (spokes). Spoke accounts forward events to the hub via EventBridge. Suitable for large organizations that want centralized visibility and reduced operational overhead.

```
[Security Account - Hub]
    |
    +-- Scanner Lambda
    +-- Central EventBridge Bus
    +-- Bulk Scan Lambda
    |
    v (assumes role)
[Spoke Account A]    [Spoke Account B]    [Spoke Account C]
    |                    |                    |
    +-- EventBridge      +-- EventBridge      +-- EventBridge
    +-- CloudTrail       +-- CloudTrail       +-- CloudTrail
    +-- Spoke Role       +-- Spoke Role       +-- Spoke Role
```

## Prerequisites

- AWS CLI v2 configured with administrative permissions (or permissions to create CloudFormation stacks, Lambda, S3, DynamoDB, SNS, SQS, KMS, IAM roles, EventBridge, and Secrets Manager resources)
- Qualys subscription with API access
- Qualys access token
- For StackSet deployments: AWS Organizations with service-managed permissions enabled
- For centralized deployments: Cross-account IAM role trust relationships

## Single Account Deployment

### Quick Start

```bash
# Set required environment variable
export QUALYS_ACCESS_TOKEN="your-qualys-access-token"

# Deploy to single account
make deploy QUALYS_POD=US2 AWS_REGION=us-east-1
```

### Deployment Steps

1. Clone the repository and navigate to the project directory

2. Verify the QScanner binary exists:
   ```bash
   ls -la scanner-lambda/qscanner.gz
   ```

3. Deploy the stack:
   ```bash
   export QUALYS_ACCESS_TOKEN="your-token"
   make deploy QUALYS_POD=US2 AWS_REGION=us-east-1 STACK_NAME=qualys-scanner
   ```

4. Verify deployment:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name qualys-scanner \
     --query 'Stacks[0].Outputs' \
     --region us-east-1
   ```

### Multi-Region Deployment

Deploy to multiple regions in a single account:

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy-multi-region QUALYS_POD=US2
```

Default regions: us-east-1, us-west-2, eu-west-1. Modify the Makefile to customize.

## Multi-Account StackSet Deployment

Deploy the scanner to multiple accounts via CloudFormation StackSets. Each account receives a complete scanner installation.

### Prerequisites

1. AWS Organizations with all features enabled
2. CloudFormation StackSets service-managed permissions enabled
3. Target Organizational Units (OUs) identified

### Deployment Steps

1. Set environment variables:
   ```bash
   export QUALYS_ACCESS_TOKEN="your-token"
   export AWS_REGION="us-east-1"
   ```

2. Deploy the StackSet:
   ```bash
   make deploy-stackset \
     QUALYS_POD=US2 \
     ORG_UNIT_IDS=ou-xxxx-xxxxxxxx,ou-yyyy-yyyyyyyy
   ```

   For multiple OUs, provide a comma-separated list.

3. Monitor deployment progress:
   ```bash
   aws cloudformation list-stack-instances \
     --stack-set-name qscanner-stackset \
     --region us-east-1
   ```

### StackSet Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| QualysPod | Qualys platform region | US2 |
| QualysAccessToken | Qualys API token | (required) |
| ArtifactsBucket | S3 bucket with scanner artifacts | (auto-created) |
| EnableS3Results | Store results in S3 | true |
| EnableSNSNotifications | Send SNS notifications | true |
| EnableScanCache | Enable DynamoDB caching | true |
| EnableBulkScan | Deploy bulk scan Lambda | true |
| BulkScanSchedule | Cron schedule for bulk scans | (empty - manual only) |
| ScannerReservedConcurrency | Max concurrent scanner executions | 10 |

### Cleanup

```bash
make delete-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx
```

## Centralized Hub-Spoke Deployment

Deploy a centralized scanner in a security account that scans Lambda functions across all member accounts.

### Architecture Details

- Hub account hosts the scanner Lambda and central EventBridge bus
- Spoke accounts deploy CloudTrail and EventBridge rules that forward events to the hub
- Hub scanner assumes a role in spoke accounts to access Lambda functions
- Single Qualys credential set managed in the hub account

### Deployment Steps

#### Step 1: Deploy Hub (Security Account)

```bash
export QUALYS_ACCESS_TOKEN="your-token"
export AWS_REGION="us-east-1"

make deploy-hub \
  QUALYS_POD=US2 \
  ORG_ID=o-xxxxxxxxxx \
  STACK_NAME=qscanner
```

Note the outputs, particularly the Central EventBridge Bus ARN.

#### Step 2: Deploy Spokes (Member Accounts)

Deploy spoke infrastructure via StackSet:

```bash
make deploy-spoke-stackset \
  ORG_UNIT_IDS=ou-xxxx-xxxxxxxx
```

This deploys to all accounts in the specified OUs:
- CloudTrail trail (if not using existing)
- EventBridge rule forwarding Lambda events to hub
- IAM role for hub to assume (qualys-lambda-scanner-spoke-role)

### Hub Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| OrganizationId | AWS Organization ID for org-wide access | (required for org) |
| SpokeAccountIds | Explicit list of spoke account IDs | (alternative to OrganizationId) |
| ScannerReservedConcurrency | Max concurrent executions | 10 |
| EnableBulkScan | Deploy bulk scan Lambda | true |

### Cross-Account IAM

The spoke role (qualys-lambda-scanner-spoke-role) trusts the hub account and requires ExternalId for confused deputy protection:

```yaml
AssumeRolePolicyDocument:
  Statement:
    - Effect: Allow
      Principal:
        AWS: arn:aws:iam::HUB_ACCOUNT_ID:role/qscanner-hub-scanner-lambda-role
      Action: sts:AssumeRole
      Condition:
        StringEquals:
          sts:ExternalId: qualys-lambda-scanner
```

### Cleanup

```bash
# Delete spokes first
make delete-spoke-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx

# Then delete hub
make delete-hub
```

## Bulk Scanning Existing Functions

The event-driven scanner only processes new or updated Lambda functions. To scan existing functions, use the bulk scan Lambda.

### How It Works

1. Bulk scan Lambda lists all Lambda functions in target accounts
2. For each function, it asynchronously invokes the scanner Lambda
3. Scanner checks DynamoDB cache - skips if CodeSha256 already scanned
4. Only functions with changed code are actually scanned

### Manual Invocation

Scan current account:

```bash
aws lambda invoke \
  --function-name qualys-lambda-bulk-scan \
  --payload '{}' \
  --region us-east-1 \
  output.json

cat output.json
```

Scan specific accounts (centralized hub only):

```bash
aws lambda invoke \
  --function-name qscanner-hub-bulk-scan \
  --payload '{"account_ids": ["111111111111", "222222222222"]}' \
  --region us-east-1 \
  output.json
```

Dry run (count functions without scanning):

```bash
aws lambda invoke \
  --function-name qualys-lambda-bulk-scan \
  --payload '{"dry_run": true}' \
  --region us-east-1 \
  output.json
```

Exclude specific functions:

```bash
aws lambda invoke \
  --function-name qualys-lambda-bulk-scan \
  --payload '{"exclude_patterns": ["test-", "dev-", "staging-"]}' \
  --region us-east-1 \
  output.json
```

### Multi-Region Bulk Scan

Scan across multiple regions:

```bash
aws lambda invoke \
  --function-name qualys-lambda-bulk-scan \
  --payload '{"regions": ["us-east-1", "us-west-2", "eu-west-1"]}' \
  --region us-east-1 \
  output.json
```

Or set default regions via environment variable `DEFAULT_REGIONS` (comma-separated).

### Scheduled Bulk Scans

Enable scheduled bulk scans during deployment:

```bash
# Weekly scan every Sunday at 2:00 AM UTC
make deploy-stackset \
  QUALYS_POD=US2 \
  ORG_UNIT_IDS=ou-xxxx-xxxxxxxx \
  BulkScanSchedule="cron(0 2 ? * SUN *)"
```

Schedule expression examples:

| Expression | Description |
|------------|-------------|
| cron(0 2 ? * SUN *) | Weekly, Sunday 2:00 AM UTC |
| cron(0 0 1 * ? *) | Monthly, 1st day at midnight |
| cron(0 12 ? * MON-FRI *) | Weekdays at noon |
| rate(7 days) | Every 7 days from deployment |

### Bulk Scan Output

```json
{
  "statusCode": 200,
  "body": {
    "accounts_processed": 3,
    "accounts_failed": 0,
    "regions_scanned": 3,
    "total_functions": 5247,
    "invoked": 5247,
    "failed": 0,
    "details": [
      {
        "account": "111111111111",
        "status": "success",
        "total_functions": 2341,
        "total_invoked": 2341,
        "total_failed": 0,
        "regions": [
          {"region": "us-east-1", "status": "success", "functions": 1200, "invoked": 1200, "failed": 0},
          {"region": "us-west-2", "status": "success", "functions": 800, "invoked": 800, "failed": 0},
          {"region": "eu-west-1", "status": "success", "functions": 341, "invoked": 341, "failed": 0}
        ]
      }
    ]
  }
}
```

## Qualys Image Tagging

Automatically tag scanned images in Qualys Container Security to link them back to their source Lambda functions.

### Tag Hierarchy

```
Lambda
├── us-east-1
│   ├── arn:aws:lambda:us-east-1:123456789012:function:my-function
│   └── arn:aws:lambda:us-east-1:123456789012:function:other-function
├── us-west-2
│   └── arn:aws:lambda:us-west-2:234567890123:function:another-function
└── eu-west-1
    └── ...
```

### Enable Tagging

1. Set `EnableQualysTagging: 'true'` in CloudFormation parameters
2. Provide Qualys API credentials:
   - `QualysApiUsername`: Qualys API username
   - `QualysApiPassword`: Qualys API password

These credentials are stored in Secrets Manager alongside the access token.

### How It Works

After a successful scan:
1. Scanner extracts the image SHA256 from scan results
2. Looks up the image in Qualys CS API by SHA
3. Creates tag hierarchy if needed (Lambda → region → ARN)
4. Assigns the ARN tag to the scanned image

This enables filtering and searching images in Qualys by Lambda function ARN.

## Configuration Reference

### CloudFormation Parameters

#### Common Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| QualysPod | String | US2 | Qualys platform (US1, US2, EU1, etc.) |
| EnableS3Results | String | true | Store scan results in S3 |
| EnableSNSNotifications | String | true | Publish to SNS topic |
| EnableScanCache | String | true | Enable DynamoDB scan cache |
| CacheTTLDays | Number | 30 | Days to retain cache entries |
| ScannerMemorySize | Number | 2048 | Scanner Lambda memory (MB) |
| ScannerTimeout | Number | 900 | Scanner Lambda timeout (seconds) |
| ScannerEphemeralStorage | Number | 2048 | Scanner ephemeral storage (MB) |
| ScannerReservedConcurrency | Number | 10 | Max concurrent scanner executions |
| EnableBulkScan | String | true | Deploy bulk scan Lambda |
| BulkScanSchedule | String | (empty) | Cron expression for scheduled scans |
| BulkScanDefaultRegions | String | (empty) | Comma-separated regions for bulk scan |
| EnableQualysTagging | String | false | Tag images in Qualys CS with Lambda ARN |
| QualysApiUsername | String | (empty) | Qualys API username (for tagging) |
| QualysApiPassword | String | (empty) | Qualys API password (for tagging) |

#### Single Account Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| QualysSecretArn | String | ARN of Secrets Manager secret |
| QScannerLayerArn | String | ARN of QScanner Lambda Layer |
| CreateCloudTrail | String | Create new CloudTrail trail (default: false) |

#### StackSet Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| QualysAccessToken | String | Qualys API token (stored in Secrets Manager) |
| ArtifactsBucket | String | S3 bucket with Lambda artifacts |

#### Centralized Hub Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| OrganizationId | String | AWS Organization ID |
| SpokeAccountIds | List | Explicit spoke account IDs |

### Environment Variables

Scanner Lambda environment variables:

| Variable | Description |
|----------|-------------|
| QUALYS_SECRET_ARN | Secrets Manager secret ARN |
| RESULTS_S3_BUCKET | S3 bucket for results |
| SNS_TOPIC_ARN | SNS topic for notifications |
| SCAN_CACHE_TABLE | DynamoDB table name |
| SCAN_TIMEOUT | Scan timeout in seconds |
| CACHE_TTL_DAYS | Cache TTL in days |
| QSCANNER_PATH | Path to QScanner binary |
| CROSS_ACCOUNT_ROLE_NAME | Role name for cross-account access |
| SCANNER_EXTERNAL_ID | External ID for cross-account role assumption |
| ENABLE_QUALYS_TAGGING | Enable Qualys CS image tagging (true/false) |
| DEFAULT_REGIONS | Comma-separated regions for bulk scan |

### Secrets Manager Format

```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-access-token",
  "qualys_api_username": "your-api-username",
  "qualys_api_password": "your-api-password"
}
```

The `qualys_api_username` and `qualys_api_password` fields are only required if `EnableQualysTagging` is enabled.

## Security Features

### Encryption

- KMS customer-managed key with automatic rotation
- DynamoDB server-side encryption with KMS
- SQS queue encryption with KMS
- SNS topic encryption with KMS
- CloudWatch Logs encryption with KMS
- S3 bucket encryption with KMS

### IAM Least Privilege

- Scanner role restricted to specific resource ARNs
- Lambda tagging restricted to QualysScan* prefix only
- CloudWatch metrics restricted to QualysLambdaScanner namespace
- Cross-account role assumption requires ExternalId

### Network Security

- S3 bucket policies enforce HTTPS-only access
- Public access blocked on all S3 buckets
- No VPC required (uses AWS service endpoints)

### Audit and Compliance

- X-Ray tracing enabled for request tracking
- CloudWatch Logs with 90-day retention
- S3 versioning enabled for result artifacts
- DynamoDB point-in-time recovery enabled
- Lambda functions tagged with scan metadata

### Dead Letter Queue

Failed scanner invocations are captured in SQS DLQ for investigation and replay.

## Monitoring and Alerting

### CloudWatch Alarms

The following alarms are created when SNS notifications are enabled:

| Alarm | Threshold | Description |
|-------|-----------|-------------|
| Scanner Errors | > 5 in 5 min | Scanner Lambda errors |
| Scanner Throttles | > 1 in 5 min | Scanner being throttled |
| DLQ Messages | > 0 | Failed scans in dead letter queue |
| Scanner Duration | > 80% timeout | Scans approaching timeout |
| DynamoDB Throttling | > 0 | DynamoDB read/write throttling |
| Scanner Concurrency | > 80% reserved | Approaching concurrency limit |
| Scan Success Rate | < 90% | Low scan success rate |

### CloudWatch Metrics

Custom metrics in the QualysLambdaScanner namespace:

| Metric | Description |
|--------|-------------|
| ScansCompleted | Successful scan count |
| ScansFailed | Failed scan count |
| CacheHits | Scans skipped due to cache |
| ScanDuration | Time to complete scan |

### Lambda Tags

Scanned functions are tagged with:

| Tag | Description |
|-----|-------------|
| QualysScanTimestamp | ISO 8601 timestamp of last scan |
| QualysScanStatus | success or failed |
| QualysScanTag | Qualys scan ID for correlation |

## Troubleshooting

### Scanner Not Triggering

1. Verify EventBridge rules are enabled:
   ```bash
   aws events list-rules --name-prefix qualys-lambda --region us-east-1
   ```

2. Check CloudTrail is logging Lambda events:
   ```bash
   aws cloudtrail lookup-events \
     --lookup-attributes AttributeKey=EventName,AttributeValue=CreateFunction20150331 \
     --region us-east-1 \
     --max-results 5
   ```

3. Review scanner Lambda logs:
   ```bash
   aws logs tail /aws/lambda/qualys-lambda-scanner --region us-east-1 --since 1h
   ```

Note: CloudTrail typically delivers events to EventBridge within 5-15 minutes.

### Force Immediate Scan

Trigger a scan without waiting for CloudTrail:

```bash
aws lambda update-function-configuration \
  --function-name target-function-name \
  --description "Trigger scan $(date +%s)" \
  --region us-east-1
```

### Scan Failures

1. Check scanner logs for errors:
   ```bash
   aws logs filter-log-events \
     --log-group-name /aws/lambda/qualys-lambda-scanner \
     --filter-pattern "ERROR" \
     --region us-east-1
   ```

2. Verify Qualys credentials:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id qualys-lambda-scanner-credentials \
     --region us-east-1 \
     --query SecretString \
     --output text | jq .
   ```

3. Check DLQ for failed invocations:
   ```bash
   aws sqs get-queue-attributes \
     --queue-url https://sqs.us-east-1.amazonaws.com/ACCOUNT/qualys-lambda-scanner-dlq \
     --attribute-names ApproximateNumberOfMessages \
     --region us-east-1
   ```

### Cache Issues

Clear cache for a specific function:

```bash
aws dynamodb delete-item \
  --table-name qualys-lambda-scanner-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-1:ACCOUNT:function:NAME"}}' \
  --region us-east-1
```

View cached entries:

```bash
aws dynamodb scan \
  --table-name qualys-lambda-scanner-cache \
  --region us-east-1 \
  --projection-expression "function_arn,scan_timestamp,code_sha256" \
  --output table
```

### Cross-Account Issues

1. Verify spoke role exists and has correct trust policy:
   ```bash
   aws iam get-role --role-name qualys-lambda-scanner-spoke-role
   ```

2. Test role assumption from hub account:
   ```bash
   aws sts assume-role \
     --role-arn arn:aws:iam::SPOKE_ACCOUNT:role/qualys-lambda-scanner-spoke-role \
     --role-session-name test \
     --external-id qualys-lambda-scanner
   ```

## Maintenance

### Update Scanner Code

```bash
make update-function AWS_REGION=us-east-1
```

### Update StackSet

```bash
make deploy-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx
```

This updates existing stack instances with new code and configuration.

### Rebuild Layer

If updating the QScanner binary:

```bash
# Replace the binary
cp new-qscanner scanner-lambda/qscanner.gz

# Rebuild and deploy
make layer
make deploy AWS_REGION=us-east-1
```

### Clean Build Artifacts

```bash
make clean
```

### Delete Deployment

Single account:
```bash
make delete AWS_REGION=us-east-1
```

StackSet:
```bash
make delete-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx
```

Hub-spoke:
```bash
make delete-spoke-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx
make delete-hub
```

## Supported Qualys Platforms

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py      # Scanner Lambda code
│   ├── bulk_scan.py            # Bulk scan Lambda code
│   ├── qscanner.gz             # QScanner binary (compressed)
│   └── requirements.txt        # Python dependencies
├── cloudformation/
│   ├── single-account-native.yaml   # Single account deployment
│   ├── stackset.yaml                # Multi-account StackSet
│   ├── centralized-hub.yaml         # Hub account scanner
│   └── centralized-spoke.yaml       # Spoke account forwarder
├── terraform/
│   ├── modules/
│   │   └── scanner-native/     # Terraform module
│   └── examples/
│       └── single-region-native/
├── Makefile                    # Deployment automation
└── README.md
```

## License

Copyright 2025. All rights reserved.

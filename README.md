# Qualys Lambda Scanner

Automated security scanning for AWS Lambda functions using Qualys QScanner. Triggers scans when Lambda functions are created or updated, with results sent to Qualys Container Security.

## Quick Start

```bash
# Set Qualys access token
export QUALYS_ACCESS_TOKEN="your-qualys-access-token"

# Deploy (basic)
make deploy QUALYS_POD=US2

# Deploy with Qualys image tagging (recommended)
make deploy QUALYS_POD=US2 TAG=true USERNAME=your-username PASSWORD='your-password'
```

## What Gets Deployed

| Resource | Purpose |
|----------|---------|
| Scanner Lambda | Executes QScanner against target Lambda functions |
| Lambda Layer | Contains the QScanner binary |
| Bulk Scan Lambda | Scans all existing Lambda functions on-demand |
| DynamoDB Table | Caches scan results to avoid duplicate scans |
| S3 Bucket | Stores scan result artifacts |
| SNS Topic | Publishes scan notifications |
| EventBridge Rules | Triggers scanner on Lambda create/update events |
| Secrets Manager | Stores Qualys credentials |
| KMS Key | Encrypts all data at rest |

## Prerequisites

1. **AWS CLI** configured with permissions to create CloudFormation stacks
2. **Qualys subscription** with Container Security module
3. **Qualys Access Token** - generate from Qualys Console
4. **QScanner binary** - place `qscanner.gz` in `scanner-lambda/` directory

## Deployment

### Basic Deployment

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy QUALYS_POD=US2
```

### With Qualys Image Tagging

Tags scanned images in Qualys Container Security with the Lambda function ARN for traceability:

```bash
make deploy QUALYS_POD=US2 TAG=true USERNAME=your-api-user PASSWORD='your-api-password'
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `QUALYS_POD` | US2 | Qualys platform: US1, US2, US3, US4, EU1, EU2, CA1, etc. |
| `AWS_REGION` | us-east-1 | AWS region to deploy to |
| `STACK_NAME` | qualys-lambda-scanner | CloudFormation stack name |
| `TAG` | false | Enable Qualys image tagging |
| `USERNAME` | - | Qualys API username (required if TAG=true) |
| `PASSWORD` | - | Qualys API password (required if TAG=true) |

## How It Works

1. Lambda function is created or updated
2. CloudTrail logs the API call
3. EventBridge rule triggers the Scanner Lambda
4. Scanner downloads the Lambda code and runs QScanner
5. Results are uploaded to Qualys and stored in S3
6. Lambda function is tagged with scan status

**Note:** CloudTrail events typically take 5-15 minutes to propagate to EventBridge.

## Bulk Scanning Existing Functions

The event-driven scanner only catches new/updated functions. To scan existing functions:

```bash
# Scan all functions in current region
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{}' \
  output.json

# Dry run (count only)
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"dry_run": true}' \
  output.json

# Scan multiple regions
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"regions": ["us-east-1", "us-west-2", "eu-west-1"]}' \
  output.json
```

## Lambda Tags Applied

After scanning, the target Lambda function is tagged:

| Tag | Example Value |
|-----|---------------|
| `QualysScanTimestamp` | 2025-01-15T10:30:00Z |
| `QualysScanStatus` | success, partial, or failed |
| `QualysScanTag` | Lambda/us-east-1/arn:aws:lambda:... |

## Troubleshooting

### Check Scanner Logs

```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --since 1h
```

### Force a Scan (Without Waiting for CloudTrail)

```bash
aws lambda update-function-configuration \
  --function-name your-function-name \
  --description "Trigger scan $(date +%s)"
```

### Clear Cache for a Function

```bash
aws dynamodb delete-item \
  --table-name qualys-lambda-scanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-1:123456789012:function:my-function"}}'
```

### Verify Credentials

```bash
aws secretsmanager get-secret-value \
  --secret-id qualys-lambda-scanner-qualys-credentials \
  --query SecretString --output text | jq .
```

## Updating

### Update Lambda Code Only

```bash
make update-function
```

### Full Redeploy

```bash
make deploy QUALYS_POD=US2 TAG=true USERNAME=user PASSWORD='pass'
```

## Cleanup

```bash
make delete
```

**Note:** You may need to empty the S3 bucket first if it contains scan results.

## Multi-Account Deployment

For multi-account deployments using hub-spoke architecture or StackSets, see the CloudFormation templates in `cloudformation/`:

- `centralized-hub.yaml` - Central scanner in security account
- `centralized-spoke.yaml` - Event forwarding from member accounts
- `stackset.yaml` - Deploy scanner to multiple accounts via StackSet

## Supported Qualys Platforms

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## Project Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py   # Scanner Lambda code
│   ├── bulk_scan.py         # Bulk scan Lambda code
│   └── qscanner.gz          # QScanner binary (you provide)
├── cloudformation/
│   └── single-account-native.yaml
├── Makefile
└── README.md
```

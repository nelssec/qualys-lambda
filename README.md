# Qualys Lambda Scanner

Automated security scanning for AWS Lambda functions using Qualys QScanner.

## Quick Start

```bash
export QUALYS_ACCESS_TOKEN="your-qualys-access-token"

# Basic deployment (Lambda tagging enabled by default)
make deploy QUALYS_POD=US2

# Disable Lambda tagging
make deploy QUALYS_POD=US2 TAG=false
```

## Resources Deployed

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

1. AWS CLI configured with CloudFormation permissions
2. Qualys subscription with Container Security module
3. Qualys Access Token from Qualys Console
4. QScanner binary in `scanner-lambda/qscanner.gz`

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `QUALYS_POD` | US2 | Qualys platform |
| `AWS_REGION` | us-east-1 | AWS region |
| `STACK_NAME` | qualys-lambda-scanner | CloudFormation stack name |
| `TAG` | true | Enable AWS Lambda resource tagging |

### Tagging Options

Lambda tagging is enabled by default. When enabled, the scanner applies AWS resource tags to Lambda functions after each scan.

| Option | Environment Variable | Description |
|--------|---------------------|-------------|
| Lambda Tagging | `ENABLE_TAGGING` | Applies AWS resource tags to Lambda functions |

#### Examples

```bash
# Standard deployment with Lambda tagging enabled (default)
make deploy QUALYS_POD=US2

# Disable Lambda tagging (for customers with policies that forbid Lambda tags)
make deploy QUALYS_POD=US2 TAG=false
```

#### CloudFormation Parameters

When deploying directly with CloudFormation:

```bash
aws cloudformation deploy \
  --template-file cloudformation/single-account-native.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    QualysPod=US2 \
    EnableTagging=true \
    QualysSecretArn=arn:aws:secretsmanager:... \
  --capabilities CAPABILITY_IAM
```

## How It Works

1. Lambda function is created or updated
2. CloudTrail logs the API call
3. EventBridge rule triggers the Scanner Lambda
4. Scanner downloads the Lambda code and runs QScanner
5. Results are uploaded to Qualys and stored in S3
6. Lambda function is tagged with scan status

CloudTrail events typically take 5-15 minutes to propagate to EventBridge.

## Bulk Scanning

Scan existing Lambda functions:

```bash
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{}' \
  output.json

# Dry run
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"dry_run": true}' \
  output.json

# Multiple regions
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"regions": ["us-east-1", "us-west-2", "eu-west-1"]}' \
  output.json
```

## Lambda Tags

| Tag | Example |
|-----|---------|
| `QualysScanTimestamp` | 2025-01-15T10:30:00Z |
| `QualysScanStatus` | success, partial, failed |

## Troubleshooting

Check scanner logs:
```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --since 1h
```

Force a scan without waiting for CloudTrail:
```bash
aws lambda update-function-configuration \
  --function-name your-function-name \
  --description "Trigger scan $(date +%s)"
```

Clear cache for a function:
```bash
aws dynamodb delete-item \
  --table-name qualys-lambda-scanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-1:123456789012:function:my-function"}}'
```

## Updating

Update Lambda code only:
```bash
make update-function
```

Full redeploy:
```bash
make deploy QUALYS_POD=US2
```

## Cleanup

```bash
make delete
```

Empty the S3 bucket first if it contains scan results.

## Multi-Region Deployment

```bash
make deploy-multi-region QUALYS_POD=US2
```

Or deploy to each region individually:
```bash
make deploy QUALYS_POD=US2 AWS_REGION=us-east-1
make deploy QUALYS_POD=US2 AWS_REGION=us-west-2
make deploy QUALYS_POD=US2 AWS_REGION=eu-west-1
```

## Multi-Account Deployment

### StackSet

Deploy a standalone scanner to all accounts in your AWS Organization:

```bash
export QUALYS_ACCESS_TOKEN="your-token"

make deploy-stackset QUALYS_POD=US2 \
  ORG_UNIT_IDS="ou-xxxx-xxxxxxxx,ou-yyyy-yyyyyyyy"
```

### Hub-Spoke

Deploy a central scanner in a security account that scans Lambda functions in member accounts.

Deploy the hub:
```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy-hub QUALYS_POD=US2
```

Deploy spokes to member accounts:
```bash
make deploy-spoke-stackset \
  ORG_UNIT_IDS="ou-xxxx-xxxxxxxx" \
  HUB_EVENT_BUS_ARN="arn:aws:events:us-east-1:SECURITY_ACCOUNT:event-bus/qualys-scanner-hub"
```

### CloudFormation Templates

| Template | Description |
|----------|-------------|
| `single-account-native.yaml` | Standalone scanner |
| `stackset.yaml` | StackSet deployment |
| `centralized-hub.yaml` | Hub scanner |
| `centralized-spoke.yaml` | Spoke event forwarding |

## Supported Platforms

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## Project Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py
│   ├── bulk_scan.py
│   └── qscanner.gz
├── cloudformation/
│   ├── single-account-native.yaml
│   ├── stackset.yaml
│   ├── centralized-hub.yaml
│   └── centralized-spoke.yaml
├── Makefile
└── README.md
```

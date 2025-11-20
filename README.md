# Qualys Lambda Scanner

Automated scanning of AWS Lambda functions using Qualys QScanner. Triggered by EventBridge when Lambda functions are deployed or updated.

## How It Works

1. Lambda function deployed/updated
2. CloudTrail logs API call
3. EventBridge triggers scanner Lambda
4. Scanner executes qscanner binary against target Lambda
5. Results sent to Qualys, stored in S3, published to SNS

## Architecture

### Event-Driven Scanning
EventBridge rules capture Lambda API events from CloudTrail:
- CreateFunction20150331
- UpdateFunctionCode20150331v2
- UpdateFunctionConfiguration20150331v2

### Scanner Lambda
Python Lambda function with QScanner binary deployed as Lambda Layer. Executes QScanner against target Lambda functions and stores results.

### Deployment Models

**Single Account**: Scanner deployed in one account, scans Lambdas in that account.

**Multi-Account StackSet**: Scanner deployed to each account via CloudFormation StackSet. Each account has independent scanner.

**Centralized Hub-Spoke**: Single scanner in security account. Spoke accounts forward Lambda events to central EventBridge bus. Scanner assumes cross-account roles to scan Lambdas.

### Caching
DynamoDB stores scan results by CodeSha256 hash. If Lambda code unchanged, scan is skipped. Cache expires after configurable TTL.

### Tagging
After scanning, Lambda function is tagged with:
- QualysScanTimestamp - ISO timestamp of the scan
- QualysScanStatus - "success" or "failed"
- QualysRepoTag - RepoTag value from QScanner results (e.g., "lambdascan:1763614101")

Tags enable correlation between Lambda functions and their scan results in S3, tracking scan history, and querying by scan status.

## Deployment

### Prerequisites

- AWS CLI configured with appropriate credentials
- Qualys access token

**Note**: QScanner binary is included in the repository at `scanner-lambda/qscanner.gz` and will be automatically decompressed during deployment.

### Quick Start

1. Set your Qualys access token:
```bash
export QUALYS_ACCESS_TOKEN="your-token-here"
```

2. Deploy the scanner:
```bash
# Deploy to us-east-1 (default)
make deploy QUALYS_POD=US2

# Or deploy to a specific region
make deploy AWS_REGION=us-east-2 QUALYS_POD=US2
```

### Multi-Region Deployment

Deploy to multiple regions automatically:
```bash
make deploy-multi-region QUALYS_POD=US2
```

This deploys to us-east-1, us-west-2, and eu-west-1 by default.

### What Gets Deployed

**CloudFormation Stack**: `qscanner`

**Lambda Function**: `qscanner`
- Runtime: Python 3.11
- Memory: 2048 MB
- Timeout: 900 seconds
- Ephemeral Storage: 2048 MB

**Lambda Layer**: `qscanner`
- Contains QScanner binary at `/opt/bin/qscanner`

**S3 Buckets**:
- `qscanner-scan-results-{account-id}` - Scan results storage
- `qscanner-lambda-code-{account-id}` - Lambda function code
- `qscanner-artifacts-{account-id}` - Build artifacts

**DynamoDB Table**: `qscanner-scan-cache`
- Caches scan results by function ARN and CodeSha256
- TTL enabled for automatic cleanup

**SNS Topic**: `qscanner-scan-notifications`
- Publishes scan completion notifications

**EventBridge Rules**:
- `qscanner-lambda-create` - Triggers on Lambda creation
- `qscanner-lambda-update-code` - Triggers on code updates
- `qscanner-lambda-update-config` - Triggers on configuration updates

**Secrets Manager**: `qscanner-qualys-credentials`
- Stores Qualys POD and access token

**IAM Role**: `qscanner-role`
- Permissions for Lambda, Secrets Manager, S3, SNS, DynamoDB

**CloudWatch Logs**: `/aws/lambda/qscanner`
- Scan execution logs (30 day retention)

## QScanner Command

The scanner executes:
```bash
/opt/bin/qscanner \
  --pod US2 \
  --access-token <from-secrets-manager> \
  --output-dir /tmp/qscanner-output \
  --cache-dir /tmp/qscanner-cache \
  --scan-types pkg,secret \
  lambda arn:aws:lambda:region:account:function:name
```

Scan types:
- `pkg` - Package vulnerabilities (includes OS and SCA)
- `secret` - Secret detection (API keys, credentials, tokens)

## Configuration

### Environment Variables

Scanner Lambda automatically configured with:
- `QUALYS_SECRET_ARN` - Secrets Manager ARN for credentials
- `RESULTS_S3_BUCKET` - S3 bucket name for scan results
- `SNS_TOPIC_ARN` - SNS topic ARN for notifications
- `SCAN_CACHE_TABLE` - DynamoDB table name for caching
- `SCAN_TIMEOUT` - Scan timeout in seconds (default: 300)
- `CACHE_TTL_DAYS` - Cache TTL in days (default: 30)
- `QSCANNER_PATH` - Path to QScanner binary (/opt/bin/qscanner)

### Secrets Manager Format

```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-access-token-here"
}
```

### CloudFormation Parameters

- `QualysPod` - Qualys POD (US1, US2, EU1, etc.)
- `QualysSecretArn` - ARN of existing Secrets Manager secret
- `QScannerLayerArn` - ARN of Lambda Layer with QScanner binary
- `EnableS3Results` - Create S3 bucket for results (default: true)
- `EnableSNSNotifications` - Create SNS topic (default: true)
- `EnableScanCache` - Enable DynamoDB caching (default: true)
- `CreateCloudTrail` - Create new CloudTrail (default: false)
- `CacheTTLDays` - Cache TTL in days (default: 30)
- `ScannerMemorySize` - Lambda memory in MB (default: 2048)
- `ScannerTimeout` - Lambda timeout in seconds (default: 900)

**Note on CloudTrail**: Set `CreateCloudTrail=true` only if you do not have an existing CloudTrail logging management events. EventBridge rules work with any CloudTrail in your account. Creating additional trails costs $2 per 100,000 events.

## Testing

### Create Test Lambda

```bash
# Create a simple test function
echo 'def lambda_handler(event, context): return "Hello"' > /tmp/test.py
cd /tmp && zip test.zip test.py

aws lambda create-function \
  --function-name test-lambda \
  --runtime python3.11 \
  --handler test.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::YOUR-ACCOUNT-ID:role/YOUR-LAMBDA-ROLE \
  --region us-east-2
```

### View Scanner Logs

```bash
# Follow logs in real-time
aws logs tail /aws/lambda/qscanner --region us-east-2 --follow

# View recent logs
aws logs tail /aws/lambda/qscanner --region us-east-2 --since 30m
```

### Check Scan Results

View tags on scanned Lambda:
```bash
aws lambda list-tags \
  --resource arn:aws:lambda:us-east-2:YOUR-ACCOUNT-ID:function:test-lambda \
  --region us-east-2
```

Expected output:
```json
{
  "Tags": {
    "QualysRepoTag": "lambdascan:1763622437",
    "QualysScanStatus": "success",
    "QualysScanTimestamp": "2025-11-20T07:07:35.733587"
  }
}
```

List scan results in S3:
```bash
aws s3 ls s3://qscanner-scan-results-YOUR-ACCOUNT-ID/scans/ --recursive --region us-east-2
```

Download specific scan result:
```bash
aws s3 cp s3://qscanner-scan-results-YOUR-ACCOUNT-ID/scans/test-lambda/TIMESTAMP.json ./scan-result.json --region us-east-2
```

### Manual Scan Trigger

Manually trigger a scan by updating the Lambda:
```bash
aws lambda update-function-configuration \
  --function-name test-lambda \
  --description "Trigger scan - $(date +%s)" \
  --region us-east-2
```

The scan will automatically trigger within 5-15 minutes when CloudTrail event reaches EventBridge.

## Maintenance

### Update Scanner Code

```bash
# Update just the Lambda function code
make update-function AWS_REGION=us-east-2
```

### Rebuild Layer

```bash
# Rebuild QScanner Lambda Layer
make layer
```

### Clean Build Artifacts

```bash
make clean
```

### Delete Stack

```bash
aws cloudformation delete-stack --stack-name qscanner --region us-east-2
```

## Features

- Scans all Lambda functions in account automatically
- DynamoDB caching prevents duplicate scans (by CodeSha256)
- Automatic Lambda tagging with scan results and RepoTags
- Input validation on all credentials and ARNs
- Log sanitization prevents credential leaks
- Results stored in S3 with encryption and versioning
- SNS notifications for scan completion
- CloudTrail integration for event capture
- Multi-region support
- Package vulnerability detection (OS and SCA)
- Secret detection (API keys, credentials, tokens)

## Supported Qualys PODs

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## IAM Permissions

Scanner Lambda role includes:
- `lambda:GetFunction` - Read Lambda function details
- `lambda:GetFunctionConfiguration` - Read Lambda configuration
- `lambda:TagResource` - Tag Lambda with scan results
- `secretsmanager:GetSecretValue` - Retrieve Qualys credentials
- `s3:PutObject` - Store scan results in S3
- `sns:Publish` - Send scan notifications
- `dynamodb:GetItem`, `dynamodb:PutItem` - Cache management
- `ecr:GetAuthorizationToken` - ECR authentication (for container Lambda)
- `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` - Pull container images

## Security

- Credentials stored in Secrets Manager, never in CloudFormation parameters or environment variables
- Input validation prevents command injection
- Log output sanitized to prevent credential exposure
- Least privilege IAM policies
- S3 buckets encrypted with AES256 and versioned
- Public access blocked on all S3 buckets
- DynamoDB with automatic TTL cleanup
- CloudWatch Logs with 30-day retention

## Troubleshooting

### Scanner Not Triggering

1. Check EventBridge rules are enabled:
```bash
aws events list-rules --name-prefix qscanner --region us-east-2
```

2. Verify CloudTrail is logging Lambda API calls:
```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=UpdateFunctionConfiguration20150331v2 \
  --region us-east-2 \
  --max-results 5
```

3. Check scanner Lambda logs:
```bash
aws logs tail /aws/lambda/qscanner --region us-east-2 --since 1h
```

### Scan Failures

Check specific error in logs:
```bash
aws logs filter-pattern /aws/lambda/qscanner --filter-pattern "ERROR" --region us-east-2
```

Common issues:
- Invalid Qualys credentials - Check Secrets Manager secret
- Insufficient Lambda permissions - Review IAM role policy
- QScanner binary missing - Verify Lambda Layer is attached
- Timeout - Increase Lambda timeout in CloudFormation parameters

### Cache Issues

Clear cache for specific function:
```bash
aws dynamodb delete-item \
  --table-name qscanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-2:ACCOUNT:function:NAME"}}' \
  --region us-east-2
```

View cache entries:
```bash
aws dynamodb scan \
  --table-name qscanner-scan-cache \
  --region us-east-2 \
  --query 'Items[*].[function_arn.S,scan_timestamp.S]' \
  --output table
```

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py          # Main scanner Lambda code
│   ├── qscanner.gz                 # Compressed QScanner binary
│   └── requirements.txt            # Python dependencies (boto3)
├── cloudformation/
│   ├── single-account-native.yaml  # Single account deployment (primary)
│   ├── stackset.yaml               # Multi-account StackSet deployment
│   ├── centralized-hub.yaml        # Centralized hub account scanner
│   └── centralized-spoke.yaml      # Centralized spoke account forwarder
├── terraform/
│   ├── modules/
│   │   └── scanner-native/         # Terraform module for native Lambda
│   └── examples/
│       ├── single-region-native/   # Single region example
│       └── single-account-multi-region/ # Multi-region example
├── Makefile                        # Deployment automation
├── .gitignore                      # Git ignore patterns
└── README.md                       # This file
```

## License

Copyright 2025. All rights reserved.

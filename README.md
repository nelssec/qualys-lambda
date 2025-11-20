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

- AWS CLI configured
- QScanner binary included at `scanner-lambda/qscanner`

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy AWS_REGION=us-east-1 QUALYS_POD=US2
```

### Terraform Deployment

```bash
./scripts/build-layer.sh
cd terraform/examples/single-region-native
cp terraform.tfvars.example terraform.tfvars

terraform init
terraform plan
terraform apply
```

## QScanner Command

The scanner executes:
```bash
/opt/qscanner --pod US2 --access-token <from-secrets> lambda arn:aws:lambda:region:account:function:name
```

Environment variables set:
- AWS_REGION
- QSCANNER_REGISTRY_USERNAME (optional)
- QSCANNER_REGISTRY_PASSWORD (optional)

## Configuration

Scanner Lambda environment variables:
- QUALYS_SECRET_ARN - Secrets Manager ARN
- RESULTS_S3_BUCKET - S3 bucket for results
- SNS_TOPIC_ARN - SNS topic for notifications
- SCAN_CACHE_TABLE - DynamoDB table for caching
- SCAN_TIMEOUT - Timeout in seconds (default 300)
- CACHE_TTL_DAYS - Cache TTL in days (default 30)

Secrets Manager format:
```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-token"
}
```

## Features

- Scans all Lambda functions in account
- DynamoDB caching prevents duplicate scans (by CodeSha256)
- Automatic Lambda tagging with scan results and RepoTags
- Input validation on all credentials and ARNs
- Log sanitization prevents credential leaks
- Results stored in S3 with encryption
- SNS notifications for scan completion
- CloudTrail integration for event capture
- Multi-region support

## Supported Qualys PODs

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## IAM Permissions

Scanner Lambda needs:
- lambda:GetFunction
- lambda:GetFunctionConfiguration
- lambda:TagResource
- secretsmanager:GetSecretValue
- s3:PutObject (optional)
- sns:Publish (optional)
- dynamodb:GetItem, PutItem (for cache)

## Security

- Credentials stored in Secrets Manager, never in CloudFormation parameters
- Input validation prevents command injection
- Log output sanitized to prevent credential exposure
- Least privilege IAM policies
- S3 buckets encrypted and versioned
- DynamoDB with automatic TTL cleanup

## Testing

Create a test Lambda function:
```bash
aws lambda create-function \
  --function-name test-scanner-target \
  --runtime python3.11 \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::ACCOUNT:role/execution-role
```

View scanner logs:
```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow
```

## Costs

Estimated monthly cost for 100 Lambda deployments:
- Scanner Lambda: $5
- S3 Storage: $1
- Secrets Manager: $0.40
- DynamoDB: $1
- CloudWatch Logs: $1
- Total: ~$8.40/month

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py
│   └── requirements.txt
├── cloudformation/
│   ├── single-account-native.yaml
│   ├── stackset.yaml
│   ├── centralized-hub.yaml
│   └── centralized-spoke.yaml
├── terraform/
│   ├── modules/scanner-native/
│   └── examples/single-region-native/
├── scripts/
│   └── build-layer.sh
├── Makefile
└── README.md
```

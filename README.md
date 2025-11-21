# Qualys Lambda Scanner

Event-driven security scanning for Lambda functions using Qualys QScanner. Watches for Lambda deployments via CloudTrail/EventBridge and automatically scans them.

## How It Works

CloudTrail logs Lambda API calls, EventBridge picks them up and triggers the scanner. The scanner Lambda pulls your function code, runs QScanner against it, and ships results to Qualys while storing copies in S3. It tags your Lambdas with scan metadata so you can track what's been scanned and when.

The scanner uses DynamoDB to cache results by CodeSha256. If you update a Lambda's environment variables but not the code, it won't re-scan since the hash is identical.

## What Gets Deployed

`make deploy` creates a CloudFormation stack with:

- **Lambda function** (`qscanner`) - Python 3.11, 2GB RAM, 15min timeout
- **Lambda Layer** - QScanner binary at `/opt/bin/qscanner`
- **S3 buckets** - Scan results, Lambda code, build artifacts
- **DynamoDB table** - Scan cache with TTL (30 days default)
- **SNS topic** - Scan completion notifications
- **EventBridge rules** - Trigger on CreateFunction, UpdateFunctionCode, UpdateFunctionConfiguration
- **Secrets Manager secret** - Qualys credentials (POD + access token)
- **IAM role** - Scanner permissions

The scanner tags your Lambdas after scanning:
- `QualysScanTimestamp` - When it was scanned
- `QualysScanStatus` - `success` or `failed`
- `QualysScanTag` - Qualys scan ID for correlation

## Deployment

Clone this repo and deploy. The QScanner binary is already included as `scanner-lambda/qscanner.gz` and gets decompressed automatically during the build.

**Prerequisites:**
- AWS CLI configured
- Qualys access token

**Deploy:**

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy QUALYS_POD=US2 AWS_REGION=us-east-2
```

That's it. The Makefile handles building the layer, uploading to S3, creating the secret, and deploying the stack.

**Multi-region:**

```bash
make deploy-multi-region QUALYS_POD=US2
```

Deploys to us-east-1, us-west-2, and eu-west-1 by default.

**Important: Direct CloudFormation Deployment**

The CloudFormation template (`cloudformation/single-account-native.yaml`) contains placeholder code in the Lambda function definition. **You must use the Makefile for deployment** as it handles packaging and uploading the actual Lambda function code to S3.

If you need to deploy directly with CloudFormation (without the Makefile), you must:
1. Build and upload the Lambda function code to S3 manually
2. Modify the CloudFormation template to reference the S3 bucket/key instead of using inline ZipFile code

For Terraform deployments, the module handles this automatically via the `archive_file` data source.

## Deployment Models

**Single Account** - Deploy the scanner in your account, it scans Lambdas in that account. Simple and effective.

**Multi-Account StackSet** - Deploy via CloudFormation StackSet to multiple accounts. Each account gets its own scanner instance.

**Centralized Hub-Spoke** - Deploy scanner once in a security account. Spoke accounts forward Lambda events to the central EventBridge bus, scanner assumes roles cross-account. Good for large orgs.

## QScanner Command

The scanner runs this against each Lambda:

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
- `pkg` - Package vulnerabilities (OS packages + SCA)
- `secret` - Hardcoded secrets, API keys, credentials

## Configuration

**CloudFormation Parameters:**

- `QualysPod` - Your Qualys POD (US1, US2, EU1, etc.)
- `QualysSecretArn` - Secrets Manager ARN (auto-created by Makefile)
- `QScannerLayerArn` - Layer ARN (auto-created by Makefile)
- `EnableS3Results` - Store results in S3 (default: true)
- `EnableSNSNotifications` - Publish to SNS (default: true)
- `EnableScanCache` - Use DynamoDB cache (default: true)
- `CreateCloudTrail` - Create new trail (default: false)
- `CacheTTLDays` - Cache TTL (default: 30)
- `ScannerMemorySize` - Lambda memory MB (default: 2048)
- `ScannerTimeout` - Lambda timeout seconds (default: 900)

**CloudTrail Note:** Only set `CreateCloudTrail=true` if you don't already have a trail logging management events. EventBridge works with any trail in the account. Extra trails cost $2 per 100k events.

**Secrets Manager Format:**

```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-token"
}
```

## Testing

Create a test Lambda:

```bash
echo 'def lambda_handler(event, context): return "Hello"' > /tmp/test.py
cd /tmp && zip test.zip test.py

aws lambda create-function \
  --function-name test-lambda \
  --runtime python3.11 \
  --handler test.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::ACCOUNT-ID:role/YOUR-LAMBDA-ROLE \
  --region us-east-2
```

CloudTrail typically takes 5-15 minutes to deliver events to EventBridge, so give it a bit.

**Check logs:**

```bash
aws logs tail /aws/lambda/qscanner --region us-east-2 --follow
```

**Check tags:**

```bash
aws lambda list-tags \
  --resource arn:aws:lambda:us-east-2:ACCOUNT-ID:function:test-lambda \
  --region us-east-2
```

You should see:

```json
{
  "Tags": {
    "QualysScanTag": "1763622437",
    "QualysScanStatus": "success",
    "QualysScanTimestamp": "2025-11-20T07:07:35.733587"
  }
}
```

**Check S3:**

```bash
aws s3 ls s3://qscanner-scan-results-ACCOUNT-ID/scans/ --recursive --region us-east-2
```

**Force a scan:**

CloudTrail/EventBridge can be slow. To force an immediate event:

```bash
aws lambda update-function-configuration \
  --function-name test-lambda \
  --description "Trigger scan - $(date +%s)" \
  --region us-east-2
```

This creates an UpdateFunctionConfiguration event that triggers the scanner.

## Troubleshooting

**Scanner not triggering:**

Check EventBridge rules are enabled:

```bash
aws events list-rules --name-prefix qscanner --region us-east-2
```

Verify CloudTrail is logging:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=UpdateFunctionConfiguration20150331v2 \
  --region us-east-2 \
  --max-results 5
```

Check scanner logs:

```bash
aws logs tail /aws/lambda/qscanner --region us-east-2 --since 1h
```

**Scan failures:**

Filter for errors:

```bash
aws logs filter-pattern /aws/lambda/qscanner --filter-pattern "ERROR" --region us-east-2
```

Common issues:
- Bad Qualys credentials - Check the secret in Secrets Manager
- IAM permission issues - Review the scanner role policy
- Layer not attached - Verify the Lambda has the qscanner layer
- Timeouts - Bump the timeout in CloudFormation params

**Cache issues:**

Clear cache for a specific function:

```bash
aws dynamodb delete-item \
  --table-name qscanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-2:ACCOUNT:function:NAME"}}' \
  --region us-east-2
```

View what's cached:

```bash
aws dynamodb scan \
  --table-name qscanner-scan-cache \
  --region us-east-2 \
  --query 'Items[*].[function_arn.S,scan_timestamp.S]' \
  --output table
```

## Maintenance

**Update scanner code:**

```bash
make update-function AWS_REGION=us-east-2
```

**Rebuild layer:**

```bash
make layer
```

**Clean build artifacts:**

```bash
make clean
```

**Delete stack:**

```bash
aws cloudformation delete-stack --stack-name qscanner --region us-east-2
```

## Security

- Credentials live in Secrets Manager, never in environment variables or CloudFormation parameters
- Input validation on all user-provided strings to prevent command injection
- Logs are sanitized to prevent credential leakage
- IAM policies follow least privilege
- S3 buckets use AES256 encryption, versioning enabled, public access blocked
- DynamoDB has TTL enabled for automatic cleanup
- CloudWatch logs retain for 30 days

## IAM Permissions

The scanner role needs:

- `lambda:GetFunction`, `lambda:GetFunctionConfiguration` - Read Lambda details
- `lambda:TagResource` - Tag Lambdas with scan results
- `secretsmanager:GetSecretValue` - Pull Qualys credentials
- `s3:PutObject` - Store scan results
- `sns:Publish` - Send notifications
- `dynamodb:GetItem`, `dynamodb:PutItem` - Cache lookups
- `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` - For container-based Lambdas

The ECR permissions are needed because the scanner can scan both Zip and container-based Lambda functions. If your Lambda uses an ECR image, the scanner needs to pull it.

## Supported Qualys PODs

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py          # Scanner Lambda code
│   ├── qscanner.gz                 # QScanner binary (compressed)
│   └── requirements.txt            # Python dependencies
├── cloudformation/
│   ├── single-account-native.yaml  # Single account deployment
│   ├── stackset.yaml               # Multi-account StackSet
│   ├── centralized-hub.yaml        # Hub account scanner
│   └── centralized-spoke.yaml      # Spoke account forwarder
├── terraform/
│   ├── modules/
│   │   └── scanner-native/         # Terraform module
│   └── examples/
│       ├── single-region-native/   # Single region example
│       └── single-account-multi-region/ # Multi-region example
├── Makefile                        # Deployment automation
└── README.md
```

## License

Copyright 2025. All rights reserved.

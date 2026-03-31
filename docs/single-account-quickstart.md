# Single Account Quickstart

Deploy Qualys Lambda Scanner to a single AWS account in one command.

## Prerequisites

- AWS CLI configured with admin permissions
- Qualys TotalCloud subscription with an access token
- QScanner binary at `scanner-lambda/qscanner.gz`

## Deploy

```bash
export QUALYS_ACCESS_TOKEN="your-token-here"
make quickstart QUALYS_POD=US2 AWS_REGION=us-east-1
```

That's it. This builds the Lambda layer, packages the function code, uploads artifacts to S3, and deploys a single CloudFormation stack that creates everything: the secret, the layer, the scanner, bulk scan, EventBridge rules, DynamoDB cache, S3 results bucket, SNS topic, and KMS encryption.

## Verify

Check the stack deployed successfully:

```bash
aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner \
  --query 'Stacks[0].StackStatus' \
  --region us-east-1
```

## Run a Bulk Scan

Scan all existing Lambda functions in your account:

```bash
# Dry run first - see what would be scanned
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"dry_run": true}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout

# Run the real scan
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{"regions": ["us-east-1"]}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

Bulk scan options:

| Field | Description |
|-------|-------------|
| `regions` | List of regions to scan (default: all regions) |
| `dry_run` | `true` to list functions without scanning |
| `exclude_patterns` | Function name prefixes to skip, e.g. `["test-", "dev-"]` |

## Automated Scanning

Two scanning modes are active after deployment:

- **Event-driven** - New and updated Lambda functions are scanned automatically via CloudTrail + EventBridge
- **Daily full scan** - A scheduled bulk scan runs every day at 2:00 AM UTC, catching any functions that were missed

## View Results

Scan results are stored in three places:

**Lambda tags** (quickest check):
```bash
aws lambda list-tags \
  --resource arn:aws:lambda:us-east-1:123456789012:function:my-function \
  --query 'Tags' --output table
```

Look for `QualysScanStatus` (`success`, `partial`, or `failed`) and `QualysScanTimestamp`.

**S3 bucket** (full scan artifacts):
```bash
aws s3 ls s3://qualys-lambda-scanner-scan-results-$(aws sts get-caller-identity --query Account --output text)/ --recursive
```

**CloudWatch Logs**:
```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --since 1h --follow
```

## Troubleshooting

**Bulk scan CLI timeout** - The bulk scan Lambda has a 15-minute timeout but the CLI defaults to 60 seconds. For large accounts, invoke asynchronously:
```bash
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --invocation-type Event \
  --payload '{"regions": ["us-east-1"]}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```
Then check CloudWatch Logs for results.

**Scan failures** - Check the dead letter queue for failed scans:
```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name qualys-lambda-scanner-scanner-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --query 'Attributes.ApproximateNumberOfMessages'
```

**Force re-scan** a cached function:
```bash
aws dynamodb delete-item \
  --table-name qualys-lambda-scanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-1:123456789012:function:my-function"}}'
```

## Customize

Pass additional parameters to `make quickstart`:

```bash
make quickstart QUALYS_POD=US2 \
  AWS_REGION=us-east-1 \
  STACK_NAME=my-scanner \
  TAG=false
```

Disable features via CloudFormation parameter overrides if needed - see the full parameter list in `cloudformation/single-account-native.yaml`.

## Update

Update just the Lambda code (no stack changes):
```bash
make update-function
```

Redeploy everything:
```bash
make quickstart QUALYS_POD=US2 AWS_REGION=us-east-1
```

## Cleanup

```bash
make clean-all    # Deletes stack, buckets, secrets, layers, and all resources
```

Preview what will be deleted first:
```bash
make clean-dry-run
```

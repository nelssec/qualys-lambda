# Qualys Lambda Scanner

Automated vulnerability scanning for AWS Lambda functions using Qualys QScanner.

## Prerequisites

- AWS CLI configured with appropriate permissions
- Qualys subscription with Container Security module
- Qualys Access Token
- QScanner binary (`scanner-lambda/qscanner.gz`)

## Deployment Options

### Single Account

Deploy scanner to one AWS account:

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy QUALYS_POD=US2 AWS_REGION=us-east-1
```

Disable Lambda tagging:
```bash
make deploy QUALYS_POD=US2 TAG=false
```

### Multi-Region

Deploy to multiple regions in same account:

```bash
make deploy-multi-region QUALYS_POD=US2
```

### Multi-Account (StackSet)

Deploy standalone scanner to each account via AWS Organizations:

```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy-stackset QUALYS_POD=US2 ORG_UNIT_IDS="ou-xxxx-xxxxxxxx"
```

### Hub-Spoke (Centralized)

Deploy central scanner in security account, forward events from member accounts:

1. Deploy hub in security account:
```bash
export QUALYS_ACCESS_TOKEN="your-token"
make deploy-hub QUALYS_POD=US2
```

2. Deploy spokes to member accounts:
```bash
make deploy-spoke-stackset ORG_UNIT_IDS="ou-xxxx-xxxxxxxx"
```

## Architecture

```
Lambda Create/Update Event
    │
    ▼
CloudTrail → EventBridge → Scanner Lambda
                              │
                              ├── QScanner (Zip packages)
                              │   └── qscanner lambda <arn>
                              │
                              └── QScanner (Container images)
                                  └── qscanner image <uri>
                              │
                              ▼
                         Results
                           ├── S3 (scan artifacts)
                           ├── SNS (notifications)
                           ├── DynamoDB (cache)
                           └── Lambda tags
```

## Resources Created

| Resource | Description |
|----------|-------------|
| Scanner Lambda | Executes QScanner against Lambda functions |
| Bulk Scan Lambda | Scans all existing functions on-demand |
| Lambda Layer | QScanner binary |
| DynamoDB Table | Scan cache (prevents duplicate scans) |
| S3 Bucket | Scan result artifacts |
| SNS Topic | Scan notifications |
| EventBridge Rules | Triggers on Lambda create/update |
| KMS Key | Encryption at rest |
| Secrets Manager | Qualys credentials |

## Lambda Tags Applied

| Tag | Description |
|-----|-------------|
| `QualysScanTimestamp` | ISO timestamp of scan |
| `QualysScanStatus` | `success`, `partial`, or `failed` |

## Bulk Scanning

Scan all existing Lambda functions:

```bash
aws lambda invoke \
  --function-name qualys-lambda-scanner-bulk-scan \
  --payload '{}' \
  output.json
```

Options:
```json
{
  "regions": ["us-east-1", "us-west-2"],
  "dry_run": true,
  "exclude_patterns": ["test-", "dev-"]
}
```

## Operations

Update function code only:
```bash
make update-function
```

View logs:
```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --since 1h
```

Force re-scan (clear cache):
```bash
aws dynamodb delete-item \
  --table-name qualys-lambda-scanner-scan-cache \
  --key '{"function_arn":{"S":"arn:aws:lambda:us-east-1:123456789012:function:my-function"}}'
```

## Cleanup

```bash
make delete           # Single account
make delete-stackset  # StackSet
make delete-hub       # Hub
```

## Supported Qualys Platforms

US1, US2, US3, US4, GOV1, EU1, EU2, EU3, IN1, CA1, AE1, UK1, AU1, KSA1

## Project Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py    # Scanner
│   ├── bulk_scan.py          # Bulk scanner
│   └── qscanner.gz           # QScanner binary
├── cloudformation/
│   ├── single-account-native.yaml
│   ├── stackset.yaml
│   ├── centralized-hub.yaml
│   └── centralized-spoke.yaml
├── Makefile
└── README.md
```

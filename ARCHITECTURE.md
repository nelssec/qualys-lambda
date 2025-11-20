# Lambda QScanner Pipeline Architecture

## Overview
This solution automatically scans AWS Lambda functions using Qualys QScanner whenever they are deployed or updated. It supports both single-account and multi-account (organization-wide) deployments.

## Architecture Components

### 1. Event Detection
**EventBridge Rules** capture Lambda deployment events:
- `CreateFunction20150331` - New Lambda function created
- `UpdateFunctionCode20150331v2` - Lambda function code updated
- `UpdateFunctionConfiguration20150331v2` - Lambda configuration updated

### 2. Scanner Lambda
**Docker-based Lambda** containing QScanner binary:
- Triggered by EventBridge events
- Extracts Lambda container image URI or code location
- Executes QScanner against the target
- Reports results to Qualys and optionally to S3/SNS

### 3. Deployment Models

#### Option A: Distributed (StackSet)
```
┌─────────────────────────────────────────────────────┐
│                 AWS Organization                     │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │
│  │  Account A   │  │  Account B   │  │ Account N│ │
│  │              │  │              │  │          │ │
│  │ EventBridge  │  │ EventBridge  │  │EventBridge│
│  │      ↓       │  │      ↓       │  │     ↓    │ │
│  │   Scanner    │  │   Scanner    │  │  Scanner │ │
│  │   Lambda     │  │   Lambda     │  │  Lambda  │ │
│  └──────────────┘  └──────────────┘  └──────────┘ │
└─────────────────────────────────────────────────────┘
```
- Each account has its own Scanner Lambda
- Deployed via CloudFormation StackSet
- No cross-account permissions needed
- Scales independently per account

#### Option B: Centralized
```
┌─────────────────────────────────────────────────────┐
│                 AWS Organization                     │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │
│  │  Account A   │  │  Account B   │  │ Account N│ │
│  │              │  │              │  │          │ │
│  │ EventBridge  │  │ EventBridge  │  │EventBridge│
│  │      ↓       │  │      ↓       │  │     ↓    │ │
│  │   (Event     │  │   (Event     │  │  (Event  │ │
│  │    Bus)      │  │    Bus)      │  │   Bus)   │ │
│  └──────┼───────┘  └──────┼───────┘  └─────┼────┘ │
│         └──────────────────┼─────────────────┘     │
│                            ↓                        │
│              ┌──────────────────────┐              │
│              │  Security Account    │              │
│              │                      │              │
│              │   EventBridge Bus    │              │
│              │          ↓           │              │
│              │   Scanner Lambda     │              │
│              │   (with cross-       │              │
│              │    account roles)    │              │
│              └──────────────────────┘              │
└─────────────────────────────────────────────────────┘
```
- Single Scanner Lambda in security account
- All accounts forward events to central event bus
- Requires cross-account IAM roles
- Centralized management and logging

### 4. Lambda Types Supported

#### Container-based Lambdas (Primary)
- Lambda functions deployed with container images
- QScanner scans directly from ECR
- Scanner Lambda IAM role needs ECR pull permissions

#### Zip-based Lambdas (Future Enhancement)
- Lambda functions deployed with zip files
- Would require downloading zip and scanning contents
- More complex implementation

### 5. Permissions Required

#### Scanner Lambda IAM Role:
- `ecr:GetAuthorizationToken` - Authenticate to ECR
- `ecr:BatchGetImage` - Pull images
- `ecr:GetDownloadUrlForLayer` - Download layers
- `lambda:GetFunction` - Get Lambda configuration
- `secretsmanager:GetSecretValue` - Retrieve Qualys credentials
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` - CloudWatch logging
- Optional: `s3:PutObject` - Store scan results
- Optional: `sns:Publish` - Send notifications

#### Cross-Account Role (Centralized model only):
- `sts:AssumeRole` - Assume role in target accounts
- Target account roles must trust the security account

### 6. Configuration Management

**AWS Secrets Manager** stores sensitive data:
```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-token-here",
  "registry_username": "optional",
  "registry_password": "optional"
}
```

**Environment Variables** for Scanner Lambda:
- `QUALYS_SECRET_ARN` - ARN of Secrets Manager secret
- `RESULTS_S3_BUCKET` - Optional S3 bucket for results
- `SNS_TOPIC_ARN` - Optional SNS topic for notifications
- `SCAN_TIMEOUT` - Scan timeout in seconds (default: 300)

## Scan Flow

1. Lambda function deployed/updated → EventBridge captures event
2. EventBridge triggers Scanner Lambda (or forwards to central bus)
3. Scanner Lambda:
   - Extracts Lambda function ARN and image URI from event
   - Retrieves Qualys credentials from Secrets Manager
   - Assumes cross-account role if needed (centralized model)
   - Executes QScanner with image URI
   - Parses scan results
   - Stores results in S3 and/or publishes to SNS
   - Logs to CloudWatch

## Deployment Instructions

### Prerequisites
- QScanner binary (you'll need to provide this)
- AWS CLI configured
- Appropriate AWS permissions

### Single Account Deployment
```bash
aws cloudformation deploy \
  --template-file cloudformation/single-account.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides QualysPod=US2 \
  --capabilities CAPABILITY_IAM
```

### Multi-Account StackSet Deployment
```bash
aws cloudformation create-stack-set \
  --stack-set-name qualys-lambda-scanner \
  --template-body file://cloudformation/stackset.yaml \
  --parameters ParameterKey=QualysPod,ParameterValue=US2 \
  --capabilities CAPABILITY_IAM

aws cloudformation create-stack-instances \
  --stack-set-name qualys-lambda-scanner \
  --accounts 123456789012 234567890123 \
  --regions us-east-1 us-west-2
```

### Centralized Deployment
```bash
# Deploy in security account
aws cloudformation deploy \
  --template-file cloudformation/centralized-scanner.yaml \
  --stack-name qualys-lambda-scanner-central \
  --capabilities CAPABILITY_IAM

# Deploy cross-account roles in each member account
aws cloudformation create-stack-set \
  --stack-set-name qualys-lambda-scanner-spoke \
  --template-body file://cloudformation/centralized-spoke.yaml \
  --capabilities CAPABILITY_IAM
```

## Cost Considerations

- EventBridge: Minimal (free tier covers most usage)
- Scanner Lambda: Depends on scan frequency and duration
  - Container-based Lambda: ~$0.0000166667 per GB-second
  - Ephemeral storage for large images
- Secrets Manager: ~$0.40/month per secret
- S3 storage: Minimal for scan results
- Data transfer: ECR pull costs

## Security Considerations

1. **Least Privilege**: Scanner Lambda only has permissions needed
2. **Credential Management**: Qualys tokens stored in Secrets Manager
3. **Encryption**: All data encrypted at rest and in transit
4. **Audit Logging**: All scans logged to CloudWatch
5. **Network Isolation**: Scanner can run in VPC if required
6. **Cross-Account Security**: Roles use external ID for additional security

## Future Enhancements

- Support for zip-based Lambda functions
- Integration with AWS Security Hub
- Custom webhook support for results
- Scan scheduling for periodic rescans
- Filtering rules (skip certain Lambda functions)
- Vulnerability threshold alerts
- Integration with CI/CD pipelines

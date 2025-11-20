# Qualys Lambda Scanner Pipeline

Automated security scanning for AWS Lambda functions using Qualys QScanner. Scans Lambda functions when deployed or updated.

## How It Works

1. Lambda function deployed/updated
2. CloudTrail captures Lambda API call (CreateFunction, UpdateFunctionCode, UpdateFunctionConfiguration)
3. EventBridge rule matches event and triggers scanner Lambda
4. Scanner Lambda retrieves Qualys credentials from Secrets Manager
5. Scanner Lambda executes qscanner binary against target Lambda ARN
6. QScanner downloads Lambda code (zip or container image) and scans it
7. Results sent to Qualys platform, stored in S3, and published to SNS

## Key Features

- Automatic scanning via EventBridge on Lambda deploy/update
- Supports both Zip and Container-based Lambda functions
- Native Lambda deployment (no Docker required if qscanner binary < 50MB)
- Multi-region and multi-account deployment options
- Results in S3 with SNS notifications
- Qualys credentials in Secrets Manager

## Quick Start (Native Lambda)

```bash
# Place QScanner binary
cp /path/to/qscanner scanner-lambda/

# Deploy using Makefile
export QUALYS_TOKEN="your-token"
make deploy AWS_REGION=us-east-1 QUALYS_POD=US2

# Or deploy manually
make layer
make package
aws cloudformation deploy --template-file cloudformation/single-account-native.yaml ...
```

This creates a Python Lambda function with a Lambda Layer containing qscanner binary. No Docker or ECR needed.

## Quick Start (Docker Container)

If qscanner binary is larger than 50MB or you prefer containers:

```bash
cd scanner-lambda
docker build -t qualys-lambda-scanner .
docker push ACCOUNT.dkr.ecr.REGION.amazonaws.com/qualys-lambda-scanner:latest

aws cloudformation deploy \
  --template-file cloudformation/single-account.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    ScannerImageUri=ACCOUNT.dkr.ecr.REGION.amazonaws.com/qualys-lambda-scanner:latest \
    QualysPod=US2 \
    QualysAccessToken=TOKEN \
  --capabilities CAPABILITY_NAMED_IAM
```

## Deployment Options

1. Single Account
   Scanner deployed in one account, scans Lambdas in that account
   Template: cloudformation/single-account-native.yaml or single-account.yaml

2. Multi-Account StackSet
   Scanner deployed to each account via StackSet
   Template: cloudformation/stackset.yaml

3. Centralized Hub-Spoke
   Single scanner in security account, spoke accounts forward events
   Templates: centralized-hub.yaml, centralized-spoke.yaml

## Multi-Region Deployment

Using Makefile:
```bash
make deploy-multi-region
```

Using Terraform:
```bash
cd terraform/examples/single-account-multi-region
terraform init
terraform apply
```

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/                  # Lambda function code
│   ├── lambda_function.py          # Scanner Lambda handler
│   ├── Dockerfile                  # For Docker deployment
│   └── requirements.txt
├── cloudformation/                  # CloudFormation templates
│   ├── single-account-native.yaml  # Native Lambda (no Docker)
│   ├── single-account.yaml         # Docker-based Lambda
│   ├── stackset.yaml               # Multi-account StackSet
│   ├── centralized-hub.yaml        # Hub-spoke hub
│   └── centralized-spoke.yaml      # Hub-spoke spoke
├── terraform/                       # Terraform modules
│   ├── modules/                    # Reusable modules
│   └── examples/                   # Example configurations
├── Makefile                        # Build and deploy automation
└── DEPLOYMENT_NATIVE.md            # Detailed deployment guide
```

## Binary Loading

The qscanner binary is loaded differently depending on deployment method:

Native Lambda (Layer):
- qscanner binary packaged into Lambda Layer at build time
- Layer structure: bin/qscanner
- Lambda function accesses binary at /opt/bin/qscanner
- subprocess.run(['/opt/bin/qscanner', 'lambda', 'function-arn'])

Docker Container:
- qscanner binary copied into Docker image via Dockerfile COPY
- Binary located at /opt/qscanner in container
- Lambda function accesses binary at /opt/qscanner
- subprocess.run(['/opt/qscanner', 'lambda', 'function-arn'])

No runtime download needed in either case.

## Configuration

Scanner Lambda environment variables:
- QUALYS_SECRET_ARN: Secrets Manager secret with Qualys credentials
- RESULTS_S3_BUCKET: S3 bucket for scan results (optional)
- SNS_TOPIC_ARN: SNS topic for notifications (optional)
- SCAN_TIMEOUT: Scan timeout in seconds (default 300)

Secrets Manager secret format:
```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-token"
}
```

## IAM Permissions

Scanner Lambda execution role needs:
- lambda:GetFunction - Read target Lambda configuration
- ecr:GetAuthorizationToken, ecr:BatchGetImage - Pull container images (for container Lambdas)
- secretsmanager:GetSecretValue - Read Qualys credentials
- s3:PutObject - Store scan results
- sns:Publish - Send notifications
- logs:CreateLogGroup, logs:PutLogEvents - CloudWatch logging

## Testing

Create a test Lambda:
```bash
aws lambda create-function \
  --function-name test-target \
  --runtime python3.11 \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::ACCOUNT:role/execution-role

# Watch scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow
```

## Limitations

- Scanner Lambda max timeout: 15 minutes
- Lambda Layer max size: 50MB compressed (250MB uncompressed)
- Requires CloudTrail enabled
- EventBridge rules are regional (deploy per-region if needed)
- Large Lambda packages may timeout during scan

## Costs

Estimated monthly cost for single account with 100 Lambda deployments/month:
- Scanner Lambda: $5
- EventBridge: Free
- S3 Storage: $1
- Secrets Manager: $0.40
- CloudWatch Logs: $1
- Total: ~$7.40/month

## Documentation

- DEPLOYMENT_NATIVE.md - Native Lambda deployment guide
- ARCHITECTURE.md - Detailed architecture
- docs/DEPLOYMENT.md - Step-by-step deployment
- terraform/README.md - Terraform deployment
- scanner-lambda/README.md - Lambda function details

## Troubleshooting

Scanner not triggering:
- Verify CloudTrail enabled and logging Lambda API calls
- Check EventBridge rule is enabled
- Review CloudWatch Logs for scanner Lambda

Scan failures:
- Check scanner Lambda has sufficient memory/timeout
- Verify Qualys credentials valid
- Ensure scanner Lambda has ECR/Lambda permissions
- Check qscanner binary is executable

Cross-account issues (centralized):
- Verify spoke role exists and trusts security account
- Check event forwarding rules in spoke accounts
- Verify central event bus policy allows spoke accounts

## Makefile Targets

```
make layer                 - Build QScanner Lambda Layer
make package              - Package Lambda function code
make deploy               - Deploy scanner to single region
make deploy-multi-region  - Deploy scanner to multiple regions
make update-function      - Update Lambda function code only
make clean                - Clean build artifacts
make delete               - Delete CloudFormation stack
```

## Support

- Solution issues: GitHub issues
- QScanner: Qualys Support
- AWS services: AWS Support

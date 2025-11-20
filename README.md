# Qualys Lambda Scanner

Automated scanning of AWS Lambda functions using Qualys QScanner. Triggered by EventBridge when Lambda functions are deployed or updated.

## How It Works

1. Lambda function deployed/updated
2. CloudTrail logs API call
3. EventBridge triggers scanner Lambda
4. Scanner executes qscanner binary against target Lambda
5. Results sent to Qualys, stored in S3, published to SNS

## Deployment

### Prerequisites

- QScanner binary from Qualys (Linux amd64, 37MB)
- AWS CLI configured
- Docker (optional, for container-based deployment)

### Using Lambda Layer (Recommended)

QScanner binary is 37MB, well within Lambda's 50MB layer limit. This is the simplest deployment method.

```bash
# Place binary in scanner-lambda/qscanner
export QUALYS_ACCESS_TOKEN="your-token"
make deploy AWS_REGION=us-east-1 QUALYS_POD=US2
```

### Alternative: Using Docker Container

For containerized deployment or if you prefer ECR-based distribution:

```bash
# 1. Extract and place binary
cd scanner-lambda
tar -xzf /path/to/qscanner.tar.gz
chmod +x qscanner

# 2. Build and push Docker image
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1

aws ecr create-repository --repository-name qualys-lambda-scanner --region $AWS_REGION
docker build -t qualys-lambda-scanner .
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker tag qualys-lambda-scanner:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/qualys-lambda-scanner:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/qualys-lambda-scanner:latest

# 3. Create secret
export QUALYS_ACCESS_TOKEN="your-token"
SECRET_ARN=$(aws secretsmanager create-secret \
  --name "qualys-lambda-scanner-credentials" \
  --secret-string '{"qualys_pod":"US2","qualys_access_token":"'$QUALYS_ACCESS_TOKEN'"}' \
  --region $AWS_REGION --query ARN --output text)

# 4. Deploy CloudFormation
aws cloudformation deploy \
  --template-file cloudformation/single-account.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    QualysPod=US2 \
    QualysSecretArn=$SECRET_ARN \
    ScannerImageUri=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/qualys-lambda-scanner:latest \
  --capabilities CAPABILITY_NAMED_IAM
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

## Deployment Models

### Single Account
Scanner deployed in one account, scans Lambdas in that account.
Template: `cloudformation/single-account.yaml` or `single-account-native.yaml`

### Multi-Account StackSet
Scanner deployed to each account via StackSet.
Template: `cloudformation/stackset.yaml`

### Centralized Hub-Spoke
Single scanner in security account, spoke accounts forward events.
Templates: `centralized-hub.yaml`, `centralized-spoke.yaml`

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

- Supports Zip and Container-based Lambda functions
- DynamoDB caching prevents duplicate scans of same code (by CodeSha256)
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
- ecr:GetAuthorizationToken (on *)
- ecr:BatchGetImage (on account repositories)
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

```bash
# Create test Lambda
aws lambda create-function \
  --function-name test-scanner-target \
  --runtime python3.11 \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::ACCOUNT:role/execution-role

# Watch scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow
```

## Costs

Estimated monthly cost for 100 Lambda deployments:
- Scanner Lambda: $5
- ECR Storage: $1
- S3 Storage: $1
- Secrets Manager: $0.40
- DynamoDB: $1
- CloudWatch Logs: $1
- Total: ~$9.40/month

## Repository Structure

```
qualys-lambda/
├── scanner-lambda/
│   ├── lambda_function.py
│   ├── Dockerfile
│   └── requirements.txt
├── cloudformation/
│   ├── single-account.yaml
│   ├── single-account-native.yaml
│   ├── stackset.yaml
│   ├── centralized-hub.yaml
│   └── centralized-spoke.yaml
├── terraform/
│   ├── modules/
│   └── examples/
├── Makefile
└── README.md
```

## Makefile Targets

```
make layer                 - Build QScanner Lambda Layer
make package              - Package Lambda function code
make deploy               - Deploy scanner to single region
make deploy-multi-region  - Deploy to multiple regions
make clean                - Clean build artifacts
```

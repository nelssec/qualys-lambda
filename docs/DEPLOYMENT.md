# Deployment Guide

This guide provides step-by-step instructions for deploying the Qualys Lambda Scanner in various configurations.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Preparing QScanner Binary](#preparing-qscanner-binary)
3. [Building the Scanner Lambda](#building-the-scanner-lambda)
4. [Deployment Options](#deployment-options)
   - [Single Account](#single-account-deployment)
   - [Multi-Account StackSet](#multi-account-stackset-deployment)
   - [Centralized Hub-Spoke](#centralized-hub-spoke-deployment)
5. [Post-Deployment Configuration](#post-deployment-configuration)
6. [Testing](#testing)
7. [Troubleshooting](#troubleshooting)

## Prerequisites

### Required Tools

- AWS CLI (v2.x or later)
- Docker (for container-based deployment)
- Bash shell
- jq (for JSON parsing in scripts)

### Required Permissions

You need permissions to create the following AWS resources:

- Lambda functions
- IAM roles and policies
- EventBridge rules
- Secrets Manager secrets
- S3 buckets
- SNS topics
- CloudTrail trails
- CloudFormation stacks/stacksets

### Qualys Account

- Access to Qualys Container Security
- Valid API credentials (POD and Access Token)
- QScanner binary downloaded

## Preparing QScanner Binary

1. Download QScanner binary from Qualys Support Portal
2. Place it in the `scanner-lambda` directory:

```bash
cd scanner-lambda
# Copy your downloaded qscanner binary here
cp /path/to/downloaded/qscanner .
chmod +x qscanner
```

3. Verify the binary works:

```bash
./qscanner --version
```

## Building the Scanner Lambda

### Option 1: Docker Container Image (Recommended)

This option packages the QScanner binary into a Docker container for Lambda.

```bash
cd scanner-lambda

# Set your AWS account and region
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1

# Create ECR repository
aws ecr create-repository \
  --repository-name qualys-lambda-scanner \
  --region ${AWS_REGION}

# Build Docker image
docker build -t qualys-lambda-scanner:latest .

# Authenticate to ECR
aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS --password-stdin \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Tag and push
docker tag qualys-lambda-scanner:latest \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest

docker push \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest

# Save the image URI for deployment
export SCANNER_IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest"
echo "Scanner Image URI: ${SCANNER_IMAGE_URI}"
```

### Option 2: Lambda Layer (If binary is < 50MB)

If the QScanner binary is small enough, you can use a Lambda Layer:

```bash
cd scripts
./create-lambda-layer.sh

# Publish the layer
aws lambda publish-layer-version \
  --layer-name qscanner \
  --description 'Qualys QScanner binary' \
  --zip-file fileb://../qscanner-layer.zip \
  --compatible-runtimes python3.11 python3.12 \
  --region ${AWS_REGION}

# Save the layer ARN
export QSCANNER_LAYER_ARN=$(aws lambda list-layer-versions \
  --layer-name qscanner \
  --query 'LayerVersions[0].LayerVersionArn' \
  --output text)
echo "Layer ARN: ${QSCANNER_LAYER_ARN}"
```

## Deployment Options

### Single Account Deployment

Deploy the scanner in a single AWS account to scan all Lambda functions in that account.

```bash
# Set your Qualys credentials
export QUALYS_POD=US2
read -sp "Enter Qualys Access Token: " QUALYS_TOKEN
echo

# Deploy using CloudFormation
aws cloudformation deploy \
  --template-file cloudformation/single-account.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    QualysPod=${QUALYS_POD} \
    QualysAccessToken=${QUALYS_TOKEN} \
    ScannerImageUri=${SCANNER_IMAGE_URI} \
    EnableS3Results=true \
    EnableSNSNotifications=true \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${AWS_REGION}

# Check stack status
aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner \
  --query 'Stacks[0].StackStatus' \
  --output text
```

### Multi-Account StackSet Deployment

Deploy the scanner across multiple AWS accounts using CloudFormation StackSets. Each account gets its own scanner.

#### Step 1: Create StackSet

```bash
# In your management account or delegated administrator
aws cloudformation create-stack-set \
  --stack-set-name qualys-lambda-scanner \
  --template-body file://cloudformation/stackset.yaml \
  --parameters \
    ParameterKey=QualysPod,ParameterValue=${QUALYS_POD} \
    ParameterKey=QualysAccessToken,ParameterValue=${QUALYS_TOKEN} \
    ParameterKey=ScannerImageUri,ParameterValue=${SCANNER_IMAGE_URI} \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false
```

#### Step 2: Deploy to Organizational Units or Accounts

```bash
# Option A: Deploy to specific accounts
aws cloudformation create-stack-instances \
  --stack-set-name qualys-lambda-scanner \
  --accounts 123456789012 234567890123 345678901234 \
  --regions us-east-1 us-west-2 \
  --operation-preferences \
    FailureToleranceCount=0,MaxConcurrentCount=5

# Option B: Deploy to entire OU
aws cloudformation create-stack-instances \
  --stack-set-name qualys-lambda-scanner \
  --deployment-targets \
    OrganizationalUnitIds=ou-xxxx-xxxxxxxx \
  --regions us-east-1 us-west-2 \
  --operation-preferences \
    FailureToleranceCount=1,MaxConcurrentCount=10
```

#### Step 3: Monitor Deployment

```bash
# Check StackSet operation status
aws cloudformation list-stack-set-operations \
  --stack-set-name qualys-lambda-scanner \
  --max-results 5
```

### Centralized Hub-Spoke Deployment

Deploy a single scanner in a central security account that scans Lambda functions across all accounts.

#### Architecture

- **Hub Account**: Central security account running the scanner
- **Spoke Accounts**: Member accounts forwarding events to hub

#### Step 1: Deploy Hub (in Security Account)

```bash
# In your security account
export SECURITY_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ORG_ID=$(aws organizations describe-organization --query 'Organization.Id' --output text)

aws cloudformation deploy \
  --template-file cloudformation/centralized-hub.yaml \
  --stack-name qualys-lambda-scanner-hub \
  --parameter-overrides \
    QualysPod=${QUALYS_POD} \
    QualysAccessToken=${QUALYS_TOKEN} \
    ScannerImageUri=${SCANNER_IMAGE_URI} \
    OrganizationId=${ORG_ID} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${AWS_REGION}

# Get the central event bus ARN
export CENTRAL_BUS_ARN=$(aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner-hub \
  --query 'Stacks[0].Outputs[?OutputKey==`CentralEventBusArn`].OutputValue' \
  --output text)

export CENTRAL_BUS_NAME=$(aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner-hub \
  --query 'Stacks[0].Outputs[?OutputKey==`CentralEventBusName`].OutputValue' \
  --output text)

echo "Central Event Bus ARN: ${CENTRAL_BUS_ARN}"
```

#### Step 2: Deploy Spokes (in Each Member Account)

Using StackSets (recommended):

```bash
# Create StackSet for spoke configuration
aws cloudformation create-stack-set \
  --stack-set-name qualys-lambda-scanner-spoke \
  --template-body file://cloudformation/centralized-spoke.yaml \
  --parameters \
    ParameterKey=SecurityAccountId,ParameterValue=${SECURITY_ACCOUNT_ID} \
    ParameterKey=CentralEventBusArn,ParameterValue=${CENTRAL_BUS_ARN} \
    ParameterKey=CentralEventBusName,ParameterValue=${CENTRAL_BUS_NAME} \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

# Deploy to all accounts in your organization
aws cloudformation create-stack-instances \
  --stack-set-name qualys-lambda-scanner-spoke \
  --deployment-targets \
    OrganizationalUnitIds=ou-xxxx-xxxxxxxx \
  --regions us-east-1 \
  --operation-preferences \
    FailureToleranceCount=1,MaxConcurrentCount=10
```

Or manually in each account:

```bash
# In each member account
aws cloudformation deploy \
  --template-file cloudformation/centralized-spoke.yaml \
  --stack-name qualys-lambda-scanner-spoke \
  --parameter-overrides \
    SecurityAccountId=${SECURITY_ACCOUNT_ID} \
    CentralEventBusArn=${CENTRAL_BUS_ARN} \
    CentralEventBusName=${CENTRAL_BUS_NAME} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${AWS_REGION}
```

## Post-Deployment Configuration

### Subscribe to SNS Notifications

```bash
# Get SNS topic ARN
SNS_TOPIC_ARN=$(aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`ScanNotificationsTopicArn`].OutputValue' \
  --output text)

# Subscribe email
aws sns subscribe \
  --topic-arn ${SNS_TOPIC_ARN} \
  --protocol email \
  --notification-endpoint your-email@example.com

# Subscribe Slack/Teams webhook
aws sns subscribe \
  --topic-arn ${SNS_TOPIC_ARN} \
  --protocol https \
  --notification-endpoint https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

### Configure S3 Lifecycle Policies

Scan results are automatically configured with a 90-day retention policy. To customize:

```bash
# Get bucket name
S3_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name qualys-lambda-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`ScanResultsBucketName`].OutputValue' \
  --output text)

# Update lifecycle policy
aws s3api put-bucket-lifecycle-configuration \
  --bucket ${S3_BUCKET} \
  --lifecycle-configuration file://custom-lifecycle.json
```

### Update Qualys Credentials

To rotate or update Qualys credentials:

```bash
# Update secret
aws secretsmanager update-secret \
  --secret-id qualys-lambda-scanner-qualys-credentials \
  --secret-string '{"qualys_pod":"US2","qualys_access_token":"new-token"}'
```

## Testing

### Test with a Sample Lambda

1. Create a test Lambda function:

```bash
# Create a simple Lambda using a container image
cat > Dockerfile.test <<EOF
FROM public.ecr.aws/lambda/python:3.11
CMD [ "lambda_function.handler" ]
EOF

docker build -t test-lambda -f Dockerfile.test .

# Push to ECR
aws ecr create-repository --repository-name test-lambda
aws ecr get-login-password | docker login --username AWS --password-stdin \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

docker tag test-lambda:latest \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/test-lambda:latest
docker push \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/test-lambda:latest

# Create Lambda function
aws lambda create-function \
  --function-name test-scanner-target \
  --package-type Image \
  --code ImageUri=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/test-lambda:latest \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/lambda-execution-role
```

2. Monitor CloudWatch Logs:

```bash
# Watch scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow
```

3. Check S3 for results:

```bash
# List scan results
aws s3 ls s3://${S3_BUCKET}/scans/ --recursive
```

### Verify EventBridge Rules

```bash
# List EventBridge rules
aws events list-rules --name-prefix qualys-lambda-scanner

# Check rule targets
aws events list-targets-by-rule --rule qualys-lambda-scanner-lambda-create
```

## Troubleshooting

### Scanner Lambda Not Triggered

**Check CloudTrail is enabled:**
```bash
aws cloudtrail describe-trails
aws cloudtrail get-trail-status --name qualys-lambda-scanner-trail
```

**Check EventBridge rule is enabled:**
```bash
aws events describe-rule --name qualys-lambda-scanner-lambda-create
```

**Test EventBridge rule manually:**
```bash
aws events test-event-pattern \
  --event-pattern file://event-pattern.json \
  --event file://test-event.json
```

### Scanner Execution Errors

**Check Lambda logs:**
```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/qualys-lambda-scanner-scanner \
  --filter-pattern "ERROR" \
  --max-items 10
```

**Verify IAM permissions:**
```bash
# Check Lambda execution role
aws iam get-role --role-name qualys-lambda-scanner-scanner-lambda-role

# Simulate policy
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::${AWS_ACCOUNT_ID}:role/qualys-lambda-scanner-scanner-lambda-role \
  --action-names lambda:GetFunction ecr:GetAuthorizationToken \
  --resource-arns "*"
```

**Test QScanner manually:**
```bash
# Update the Lambda and run test
aws lambda invoke \
  --function-name qualys-lambda-scanner-scanner \
  --payload file://test-payload.json \
  response.json
```

### Cross-Account Issues (Centralized Deployment)

**Verify spoke role exists:**
```bash
# In spoke account
aws iam get-role --role-name qualys-lambda-scanner-spoke-role
```

**Test assume role:**
```bash
# In hub account
aws sts assume-role \
  --role-arn arn:aws:iam::SPOKE_ACCOUNT_ID:role/qualys-lambda-scanner-spoke-role \
  --role-session-name test
```

**Check event bus policy:**
```bash
# In hub account
aws events describe-event-bus --name qualys-lambda-scanner-central-bus
```

### QScanner Timeout

If scans are timing out on large images:

1. Increase Lambda timeout (max 15 minutes)
2. Increase Lambda memory (more memory = more CPU)
3. Increase ephemeral storage if needed

```bash
aws lambda update-function-configuration \
  --function-name qualys-lambda-scanner-scanner \
  --timeout 900 \
  --memory-size 3008 \
  --ephemeral-storage Size=10240
```

## Next Steps

- [Configuration Guide](./CONFIGURATION.md)
- [Architecture Overview](../ARCHITECTURE.md)
- [Troubleshooting Guide](./TROUBLESHOOTING.md)

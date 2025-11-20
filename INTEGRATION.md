# QScanner Integration Guide

## Binary Requirements

Your QScanner binary is **>100MB** (compressed tarball), which means:

✅ **MUST use Docker container deployment**
❌ **CANNOT use Lambda Layer** (50MB compressed limit)

## Step 1: Extract and Prepare Binary

```bash
# Extract the tarball you have
cd scanner-lambda
tar -xzf /path/to/qscanner-tarball.tar.gz

# This should create a qscanner binary
ls -lh qscanner

# Make it executable
chmod +x qscanner

# Verify it's Linux amd64
file qscanner
# Should show: ELF 64-bit LSB executable, x86-64

# Test it (optional, if you have Qualys credentials)
./qscanner --version
```

## Step 2: Build Docker Image

The Dockerfile is already configured to copy the binary:

```bash
cd scanner-lambda

# Build the image
docker build -t qualys-lambda-scanner:latest .

# Check image size
docker images qualys-lambda-scanner:latest
```

## Step 3: Push to ECR

```bash
# Set your account and region
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1

# Create ECR repository (if not exists)
aws ecr create-repository \
  --repository-name qualys-lambda-scanner \
  --image-scanning-configuration scanOnPush=true \
  --encryption-configuration encryptionType=AES256 \
  --region ${AWS_REGION}

# Login to ECR
aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS --password-stdin \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Tag the image
docker tag qualys-lambda-scanner:latest \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest

# Push to ECR
docker push \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest

# Save the image URI
export SCANNER_IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest"
echo $SCANNER_IMAGE_URI
```

## Step 4: Create Qualys Secret

```bash
# Create the secret BEFORE deploying CloudFormation
export QUALYS_POD=US2
export QUALYS_ACCESS_TOKEN="your-token-here"

SECRET_ARN=$(aws secretsmanager create-secret \
  --name "qualys-lambda-scanner-credentials" \
  --description "Qualys credentials for Lambda scanner" \
  --secret-string "{\"qualys_pod\":\"${QUALYS_POD}\",\"qualys_access_token\":\"${QUALYS_ACCESS_TOKEN}\"}" \
  --region ${AWS_REGION} \
  --query ARN \
  --output text)

echo "Secret ARN: ${SECRET_ARN}"
```

## Step 5: Deploy with CloudFormation

```bash
# Deploy the Docker-based template (NOT the native one)
aws cloudformation deploy \
  --template-file cloudformation/single-account.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    QualysPod=${QUALYS_POD} \
    QualysSecretArn=${SECRET_ARN} \
    ScannerImageUri=${SCANNER_IMAGE_URI} \
    EnableS3Results=true \
    EnableSNSNotifications=true \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${AWS_REGION}
```

## Step 6: Test the Scanner

```bash
# Create a test Lambda function
cat > test_lambda.py <<EOF
def lambda_handler(event, context):
    return {"statusCode": 200, "body": "test"}
EOF

zip test_lambda.zip test_lambda.py

# Create test Lambda
aws lambda create-function \
  --function-name scanner-test-target \
  --runtime python3.11 \
  --handler test_lambda.lambda_handler \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/YOUR_LAMBDA_ROLE \
  --zip-file fileb://test_lambda.zip \
  --region ${AWS_REGION}

# Wait a few seconds, then check scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow --region ${AWS_REGION}
```

## Architecture with Docker

```
┌─────────────────────────────────────────────┐
│ Build Phase (Your Workstation)             │
│                                             │
│  1. qscanner binary (from tarball)         │
│  2. Dockerfile COPY qscanner /opt/qscanner │
│  3. docker build                            │
│  4. docker push to ECR                      │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ AWS ECR Repository                          │
│                                             │
│  qualys-lambda-scanner:latest               │
│  Size: ~120MB (uncompressed)                │
│  Encrypted at rest (AES256)                 │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ Lambda Function (Container Runtime)        │
│                                             │
│  Image: ECR URI                             │
│  /opt/qscanner ready to execute             │
│  No download at runtime                     │
│  Fast cold starts (cached layers)           │
└─────────────────────────────────────────────┘
```

## QScanner Command Executed

```bash
/opt/qscanner \
  --pod US2 \
  --access-token <from-secrets-manager> \
  lambda arn:aws:lambda:region:account:function:name
```

Environment variables set:
- `AWS_REGION` - Lambda function region
- `QSCANNER_REGISTRY_USERNAME` - if in secret (optional)
- `QSCANNER_REGISTRY_PASSWORD` - if in secret (optional)
- `QSCANNER_REGISTRY_TOKEN` - if in secret (optional)

## Multi-Region Deployment

If deploying to multiple regions, you need to:

1. **Push image to each region's ECR**:
```bash
for region in us-east-1 us-west-2 eu-west-1; do
  aws ecr create-repository \
    --repository-name qualys-lambda-scanner \
    --region ${region}

  docker tag qualys-lambda-scanner:latest \
    ${AWS_ACCOUNT_ID}.dkr.ecr.${region}.amazonaws.com/qualys-lambda-scanner:latest

  aws ecr get-login-password --region ${region} | \
    docker login --username AWS --password-stdin \
    ${AWS_ACCOUNT_ID}.dkr.ecr.${region}.amazonaws.com

  docker push \
    ${AWS_ACCOUNT_ID}.dkr.ecr.${region}.amazonaws.com/qualys-lambda-scanner:latest
done
```

2. **Deploy CloudFormation stack to each region** with region-specific image URI

OR use **ECR Replication**:
```bash
# Configure replication from us-east-1 to other regions
# See terraform/ecr-replication/main.tf for example
```

## Troubleshooting

### Binary not found in container
```bash
# Test container locally
docker run --rm -it qualys-lambda-scanner:latest ls -lh /opt/

# Should show qscanner binary
```

### Binary not executable
```bash
# Rebuild with explicit permissions
docker build -t qualys-lambda-scanner:latest .

# Check Dockerfile has: RUN chmod +x /opt/qscanner
```

### Lambda timeout during scan
```bash
# Increase Lambda timeout and memory
# Edit CloudFormation parameters:
# ScannerTimeout: 900 (15 minutes max)
# ScannerMemorySize: 3008 (or higher)
# ScannerEphemeralStorage: 10240 (if large images)
```

### ECR authentication failure
```bash
# Verify Lambda role has ECR permissions
# Check IAM policy includes:
# - ecr:GetAuthorizationToken
# - ecr:BatchGetImage
# - ecr:GetDownloadUrlForLayer
```

## Security Notes

1. **Binary Integrity**: Verify checksum before building image
2. **ECR Access**: Scanner can only access ECR in same account
3. **Credentials**: Never in environment, always from Secrets Manager
4. **Logging**: All sensitive data sanitized before CloudWatch
5. **Network**: Lambda runs in AWS-managed VPC by default

## Next Steps

After successful deployment:

1. Subscribe to SNS topic for scan notifications
2. Configure S3 lifecycle policy for scan results
3. Set up CloudWatch alarms for failures
4. Test with various Lambda package types (Zip and Container)
5. Monitor costs (Lambda invocations, ECR storage, S3)

## Cost Estimation

For 1000 Lambda deployments/month:

- Scanner Lambda: ~$10 (depends on scan duration)
- ECR Storage: ~$1 (for scanner image)
- S3 Storage: ~$1 (scan results)
- Secrets Manager: $0.40
- DynamoDB: ~$1 (on-demand)
- CloudWatch Logs: ~$2
- **Total: ~$15.40/month**

# QScanner Lambda Function

This directory contains the AWS Lambda function that performs Qualys QScanner scans on Lambda functions.

## Prerequisites

1. **QScanner Binary**: You need to obtain the QScanner binary from Qualys and place it in this directory as `qscanner` before building the Docker image.

## Building the Docker Image

```bash
# Make sure the qscanner binary is in this directory
ls -lh qscanner

# Build the Docker image
docker build -t qualys-lambda-scanner:latest .

# Test locally (optional)
docker run -p 9000:8080 qualys-lambda-scanner:latest

# In another terminal, test with a sample event
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d @test-event.json
```

## Deploying to ECR

```bash
# Set your AWS account ID and region
export AWS_ACCOUNT_ID=123456789012
export AWS_REGION=us-east-1

# Create ECR repository
aws ecr create-repository \
  --repository-name qualys-lambda-scanner \
  --region ${AWS_REGION}

# Authenticate Docker to ECR
aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Tag the image
docker tag qualys-lambda-scanner:latest \
  ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest

# Push to ECR
docker push ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/qualys-lambda-scanner:latest
```

## Environment Variables

The Lambda function uses the following environment variables:

- `QUALYS_SECRET_ARN` (required): ARN of the Secrets Manager secret containing Qualys credentials
- `RESULTS_S3_BUCKET` (optional): S3 bucket name for storing scan results
- `SNS_TOPIC_ARN` (optional): SNS topic ARN for scan notifications
- `SCAN_TIMEOUT` (optional): Scan timeout in seconds (default: 300)
- `CROSS_ACCOUNT_ROLE_ARN` (optional): Role ARN for cross-account scanning (centralized model)

## Secrets Manager Format

The Secrets Manager secret should contain:

```json
{
  "qualys_pod": "US2",
  "qualys_access_token": "your-qualys-access-token",
  "registry_username": "optional-registry-username",
  "registry_password": "optional-registry-password",
  "registry_token": "optional-registry-token"
}
```

## Testing

Create a test event file `test-event.json`:

```json
{
  "version": "0",
  "id": "12345678-1234-1234-1234-123456789012",
  "detail-type": "AWS API Call via CloudTrail",
  "source": "aws.lambda",
  "account": "123456789012",
  "time": "2023-01-01T12:00:00Z",
  "region": "us-east-1",
  "resources": [],
  "detail": {
    "eventVersion": "1.08",
    "eventName": "CreateFunction20150331",
    "eventSource": "lambda.amazonaws.com",
    "responseElements": {
      "functionArn": "arn:aws:lambda:us-east-1:123456789012:function:test-function"
    }
  }
}
```

## Lambda Configuration

Recommended Lambda configuration:
- Memory: 2048 MB (adjust based on image sizes being scanned)
- Timeout: 900 seconds (15 minutes)
- Ephemeral storage: 2048 MB (adjust based on image sizes)
- Architecture: x86_64

## IAM Permissions

The Lambda execution role needs:
- ECR pull permissions
- Lambda read permissions
- Secrets Manager read permissions
- S3 write permissions (if using results bucket)
- SNS publish permissions (if using notifications)
- CloudWatch Logs permissions

See the CloudFormation templates in the `cloudformation/` directory for complete IAM policies.

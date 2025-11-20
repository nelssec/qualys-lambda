# Native Lambda Deployment (No Docker Required)

This guide covers deploying the scanner using native Lambda (zip-based) with a Lambda Layer for the QScanner binary.

## Prerequisites

1. QScanner binary from Qualys
2. AWS CLI configured
3. Make (optional, for using Makefile)

## Architecture

The solution uses:
- Python 3.11 Lambda runtime (zip-based, not container)
- Lambda Layer containing the QScanner binary
- CloudFormation for infrastructure
- EventBridge for triggering on Lambda changes
- CloudTrail for capturing Lambda API events

## How It Works

1. QScanner binary is packaged into a Lambda Layer
2. Lambda function code is packaged as a zip file
3. CloudFormation creates the Lambda function with the Layer attached
4. When invoked, Lambda function can execute /opt/bin/qscanner (from the Layer)
5. Lambda function uses subprocess to call qscanner with the target Lambda ARN

## Quick Start with Makefile

```bash
# 1. Place QScanner binary in scanner-lambda directory
cp /path/to/qscanner scanner-lambda/

# 2. Set Qualys token
export QUALYS_TOKEN="your-token-here"

# 3. Deploy to single region
make deploy AWS_REGION=us-east-1 QUALYS_POD=US2

# 4. Deploy to multiple regions
make deploy-multi-region QUALYS_POD=US2
```

## Manual Deployment Steps

If you prefer not to use Make:

### 1. Build Lambda Layer

```bash
# Create layer directory structure
mkdir -p build/layer/bin
cp scanner-lambda/qscanner build/layer/bin/
chmod +x build/layer/bin/qscanner

# Create layer zip
cd build/layer
zip -r ../qscanner-layer.zip .
cd ../..

# Publish to AWS
aws lambda publish-layer-version \
  --layer-name qscanner \
  --description "Qualys QScanner binary" \
  --zip-file fileb://build/qscanner-layer.zip \
  --compatible-runtimes python3.11 python3.12 \
  --region us-east-1

# Save the returned LayerVersionArn
```

### 2. Package Lambda Function

```bash
# Create function zip
mkdir -p build/function
cp scanner-lambda/lambda_function.py build/function/
cd build/function
zip -r ../scanner-function.zip .
cd ../..

# Upload to S3 (CloudFormation needs it there)
aws s3 mb s3://my-lambda-artifacts
aws s3 cp build/scanner-function.zip s3://my-lambda-artifacts/
```

### 3. Deploy CloudFormation

```bash
aws cloudformation deploy \
  --template-file cloudformation/single-account-native.yaml \
  --stack-name qualys-lambda-scanner \
  --parameter-overrides \
    QualysPod=US2 \
    QualysAccessToken=YOUR_TOKEN \
    QScannerLayerArn=arn:aws:lambda:us-east-1:ACCOUNT:layer:qscanner:1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

## Multi-Region Deployment

For multi-region, you need to:
1. Publish the Lambda Layer to each region
2. Upload function code to S3 in each region (or use the same global bucket)
3. Deploy CloudFormation stack to each region

Using the Makefile:

```bash
make deploy-multi-region
```

This will automatically deploy to us-east-1, us-west-2, and eu-west-1.

## Lambda Layer Size Limit

Lambda Layers have a 50MB compressed limit (250MB uncompressed).

Check your layer size:

```bash
du -h build/qscanner-layer.zip
```

If the QScanner binary is larger than 50MB compressed, you must use the Docker container deployment method instead (see cloudformation/single-account.yaml).

## Updating Function Code

To update just the Lambda function code without redeploying the entire stack:

```bash
make update-function
```

Or manually:

```bash
aws lambda update-function-code \
  --function-name qualys-lambda-scanner-scanner \
  --s3-bucket my-lambda-artifacts \
  --s3-key scanner-function.zip \
  --region us-east-1
```

## Updating QScanner Binary

To update the QScanner binary:

1. Replace scanner-lambda/qscanner with new version
2. Rebuild and publish the layer:

```bash
make publish-layer
```

3. Update Lambda function to use new layer version:

```bash
aws lambda update-function-configuration \
  --function-name qualys-lambda-scanner-scanner \
  --layers arn:aws:lambda:us-east-1:ACCOUNT:layer:qscanner:NEW_VERSION \
  --region us-east-1
```

## Testing

Create a test Lambda function to trigger the scanner:

```bash
# Create test function (use a simple runtime)
aws lambda create-function \
  --function-name test-scanner-trigger \
  --runtime python3.11 \
  --role arn:aws:iam::ACCOUNT:role/some-execution-role \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://test.zip \
  --region us-east-1

# Check scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow --region us-east-1
```

## Comparison: Native vs Docker

Native Lambda (this guide):
- Pros: Simpler deployment, no Docker/ECR needed, faster cold starts
- Cons: 50MB layer size limit, less flexibility

Docker Container:
- Pros: No size limits, full control of environment
- Cons: Requires Docker, ECR, more complex deployment

Choose native Lambda if your QScanner binary is under 50MB compressed. Otherwise use Docker container deployment.

## Terraform Alternative

See terraform/examples/native-lambda/ for Terraform-based deployment.

## Cleanup

Delete the stack:

```bash
make delete
```

Or manually:

```bash
aws cloudformation delete-stack --stack-name qualys-lambda-scanner --region us-east-1
```

Note: S3 buckets must be emptied before deletion.

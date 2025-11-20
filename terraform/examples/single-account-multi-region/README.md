# Multi-Region Single Account Deployment

Deploy the Qualys Lambda Scanner across multiple AWS regions in a single account.

## Prerequisites

1. QScanner binary
2. Docker images built and pushed to ECR in each target region
3. AWS credentials configured

## Building and Replicating Docker Images

You need to build the Docker image once and replicate it to each region's ECR:

```bash
# Build image
cd scanner-lambda
docker build -t qualys-lambda-scanner:latest .

# Push to primary region
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
aws ecr create-repository --repository-name qualys-lambda-scanner --region us-east-1
docker tag qualys-lambda-scanner:latest ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest
docker push ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest

# Replicate to other regions using ECR replication or manual push
# Option 1: Enable ECR replication (recommended)
aws ecr put-replication-configuration --replication-configuration file://replication-config.json

# Option 2: Manual push to each region
for region in us-west-2 eu-west-1; do
  aws ecr create-repository --repository-name qualys-lambda-scanner --region $region
  aws ecr get-login-password --region $region | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.$region.amazonaws.com
  docker tag qualys-lambda-scanner:latest ACCOUNT_ID.dkr.ecr.$region.amazonaws.com/qualys-lambda-scanner:latest
  docker push ACCOUNT_ID.dkr.ecr.$region.amazonaws.com/qualys-lambda-scanner:latest
done
```

## Deployment

1. Copy the example tfvars file:
```bash
cp terraform.tfvars.example terraform.tfvars
```

2. Edit terraform.tfvars with your values:
```hcl
regions = ["us-east-1", "us-west-2", "eu-west-1"]
qualys_pod = "US2"
scanner_image_uri_map = {
  "us-east-1" = "ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest"
  "us-west-2" = "ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/qualys-lambda-scanner:latest"
  "eu-west-1" = "ACCOUNT_ID.dkr.ecr.eu-west-1.amazonaws.com/qualys-lambda-scanner:latest"
}
```

3. Set Qualys token as environment variable:
```bash
export TF_VAR_qualys_access_token="your-token-here"
```

4. Deploy:
```bash
terraform init
terraform plan
terraform apply
```

## What Gets Deployed Per Region

Each region deployment includes:
- Scanner Lambda function (container-based)
- EventBridge rules for Lambda create/update events
- CloudTrail trail (for capturing Lambda API calls)
- Secrets Manager secret (with Qualys credentials)
- S3 bucket (for scan results)
- SNS topic (for notifications)
- IAM roles and policies

## Outputs

After deployment, Terraform outputs the Scanner Lambda ARN for each region:

```bash
terraform output scanner_deployments
```

## Testing

Create a Lambda function in any deployed region to trigger a scan:

```bash
aws lambda create-function \
  --region us-east-1 \
  --function-name test-scan-target \
  --package-type Image \
  --code ImageUri=public.ecr.aws/lambda/python:3.11 \
  --role arn:aws:iam::ACCOUNT_ID:role/some-role

# Check scanner logs
aws logs tail /aws/lambda/qualys-lambda-scanner-us-east-1-scanner --region us-east-1 --follow
```

## Cleanup

```bash
terraform destroy
```

Note: You may need to manually empty S3 buckets before Terraform can delete them.

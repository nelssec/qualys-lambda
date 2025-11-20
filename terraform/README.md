# Terraform Deployment

Terraform modules for deploying the Qualys Lambda Scanner.

## Directory Structure

```
terraform/
├── modules/
│   ├── scanner-single-account/    # Single account scanner module
│   ├── scanner-stackset/          # Multi-account StackSet module
│   ├── scanner-centralized-hub/   # Centralized hub module
│   └── scanner-centralized-spoke/ # Centralized spoke module
├── examples/
│   └── single-account-multi-region/ # Example: multi-region deployment
└── ecr-replication/               # ECR cross-region replication setup
```

## Quick Start

### 1. Build and Push Docker Image

The QScanner binary must be packaged into a Docker image and pushed to ECR before deploying the scanner Lambda.

```bash
cd scanner-lambda

# Ensure qscanner binary is present
ls -lh qscanner

# Build image
docker build -t qualys-lambda-scanner:latest .

# Push to ECR (replace ACCOUNT_ID and REGION)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-east-1

aws ecr create-repository --repository-name qualys-lambda-scanner --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker tag qualys-lambda-scanner:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/qualys-lambda-scanner:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/qualys-lambda-scanner:latest
```

### 2. Deploy Using Terraform

#### Single Region

```hcl
module "scanner" {
  source = "../../modules/scanner-single-account"

  region              = "us-east-1"
  qualys_pod          = "US2"
  qualys_access_token = var.qualys_access_token
  scanner_image_uri   = "ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest"
}
```

#### Multi Region

See examples/single-account-multi-region/ for a complete multi-region example.

```bash
cd examples/single-account-multi-region
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values
export TF_VAR_qualys_access_token="your-token"
terraform init
terraform apply
```

## How It Works

1. Build Phase (One-time)
   - QScanner binary is copied into Docker image via Dockerfile COPY command
   - Image is built and pushed to ECR
   - Image is optionally replicated to other regions using ECR replication

2. Deploy Phase
   - Terraform calls CloudFormation with the ECR image URI
   - CloudFormation creates Lambda function using the container image
   - EventBridge rules are created to trigger the Lambda

3. Runtime Phase
   - Lambda function deployed receives events from EventBridge
   - Lambda container starts with QScanner binary at /opt/qscanner
   - Lambda executes QScanner against target Lambda function
   - Results are stored in S3 and sent to SNS

## Binary Loading Explained

The QScanner binary is loaded into the Lambda container at build time:

1. Dockerfile COPY instruction copies qscanner to /opt/qscanner
2. Docker build creates image layers containing the binary
3. Image is pushed to ECR
4. Lambda function references the ECR image URI
5. When Lambda invokes, the container starts with the binary already present
6. Python code executes the binary using subprocess.run()

No runtime download or installation is needed.

## Multi-Region Deployment

For multi-region deployments, you need the Docker image in each region's ECR.

Option 1: ECR Replication (Recommended)

```bash
cd terraform/ecr-replication
terraform apply -var="primary_region=us-east-1" -var='replication_regions=["us-west-2","eu-west-1"]'

# Push image to primary region only, it will replicate automatically
docker push $ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest
```

Option 2: Manual Push

Push the image to each region manually. See examples/single-account-multi-region/README.md for details.

## Module Inputs

See each module's variables.tf for full list.

Common variables:
- region: AWS region
- qualys_pod: Qualys POD (US1, US2, etc)
- qualys_access_token: Qualys API token
- scanner_image_uri: ECR image URI
- enable_s3_results: Create S3 bucket for results
- enable_sns_notifications: Create SNS topic
- scanner_memory_size: Lambda memory in MB
- scanner_timeout: Lambda timeout in seconds

## State Management

For production, use remote state:

```hcl
terraform {
  backend "s3" {
    bucket = "your-terraform-state-bucket"
    key    = "qualys-lambda-scanner/terraform.tfstate"
    region = "us-east-1"
  }
}
```

## Cleanup

```bash
terraform destroy
```

Note: S3 buckets must be empty before deletion. CloudFormation will fail to delete non-empty buckets.

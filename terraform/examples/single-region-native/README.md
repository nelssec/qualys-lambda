## Qualys Lambda Scanner - Single Region Deployment (Native)

This example deploys the Qualys Lambda Scanner using native Lambda with Lambda Layer for the QScanner binary.

### Prerequisites

1. QScanner binary (37MB) downloaded from Qualys
2. AWS CLI configured
3. Terraform >= 1.0 installed

### Deployment Steps

#### 1. Extract QScanner Binary

```bash
cd ../../../scanner-lambda
tar -xzf /path/to/qscanner.tar.gz
chmod +x qscanner
cd -
```

#### 2. Build Lambda Layer

```bash
cd ../../..
./scripts/build-layer.sh
```

This creates `build/qscanner-layer.zip` (under 50MB).

#### 3. Configure Variables

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and set your values:

```hcl
aws_region          = "us-east-1"
stack_name          = "qualys-lambda-scanner"
qualys_pod          = "US2"
qualys_access_token = "your-qualys-access-token"
qscanner_layer_zip  = "../../../build/qscanner-layer.zip"
```

#### 4. Deploy with Terraform

```bash
terraform init
terraform plan
terraform apply
```

### What Gets Deployed

- Lambda function with QScanner layer
- Lambda Layer containing QScanner binary (37MB)
- Secrets Manager secret for Qualys credentials
- DynamoDB table for scan caching
- S3 bucket for scan results
- SNS topic for scan notifications
- CloudTrail for capturing Lambda events
- EventBridge rules for triggering scans
- IAM roles and policies

### Testing

After deployment, create or update a Lambda function to trigger a scan:

```bash
aws lambda create-function \
  --function-name test-scanner-target \
  --runtime python3.11 \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://test.zip \
  --role arn:aws:iam::ACCOUNT:role/execution-role
```

Watch scanner logs:

```bash
aws logs tail /aws/lambda/qualys-lambda-scanner-scanner --follow
```

### Outputs

After deployment, Terraform outputs:

- `scanner_lambda_arn` - ARN of the scanner Lambda function
- `scanner_lambda_name` - Name of the scanner Lambda function
- `qscanner_layer_arn` - ARN of the QScanner Lambda Layer
- `qualys_secret_arn` - ARN of Qualys credentials secret
- `scan_results_bucket` - S3 bucket name for scan results
- `scan_notifications_topic` - SNS topic ARN for notifications
- `scan_cache_table` - DynamoDB table name for caching
- `cloudtrail_name` - CloudTrail trail name

### Cleanup

```bash
terraform destroy
```

Note: You may need to manually empty S3 buckets before destroying.

### Multi-Region Deployment

To deploy across multiple regions, use the same module with different provider configurations:

```hcl
provider "aws" {
  alias  = "us-east-1"
  region = "us-east-1"
}

provider "aws" {
  alias  = "us-west-2"
  region = "us-west-2"
}

module "scanner_us_east_1" {
  source = "../../modules/scanner-native"
  providers = {
    aws = aws.us-east-1
  }
  # ... configuration
}

module "scanner_us_west_2" {
  source = "../../modules/scanner-native"
  providers = {
    aws = aws.us-west-2
  }
  # ... configuration
}
```

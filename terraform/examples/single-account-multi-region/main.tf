terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

# Example: Deploy scanner in multiple regions

variable "regions" {
  description = "List of AWS regions to deploy the scanner"
  type        = list(string)
  default     = ["us-east-1", "us-west-2", "eu-west-1"]
}

variable "qualys_pod" {
  description = "Qualys POD"
  type        = string
  default     = "US2"
}

variable "qualys_access_token" {
  description = "Qualys Access Token"
  type        = string
  sensitive   = true
}

variable "scanner_image_uri_map" {
  description = "Map of region to Scanner ECR image URI"
  type        = map(string)
  # Example:
  # {
  #   "us-east-1" = "123456789012.dkr.ecr.us-east-1.amazonaws.com/qualys-lambda-scanner:latest"
  #   "us-west-2" = "123456789012.dkr.ecr.us-west-2.amazonaws.com/qualys-lambda-scanner:latest"
  # }
}

# Configure providers for each region
provider "aws" {
  alias  = "us-east-1"
  region = "us-east-1"
}

provider "aws" {
  alias  = "us-west-2"
  region = "us-west-2"
}

provider "aws" {
  alias  = "eu-west-1"
  region = "eu-west-1"
}

# Deploy scanner in each region
module "scanner_us_east_1" {
  source = "../../modules/scanner-single-account"
  count  = contains(var.regions, "us-east-1") ? 1 : 0

  providers = {
    aws = aws.us-east-1
  }

  region              = "us-east-1"
  qualys_pod          = var.qualys_pod
  qualys_access_token = var.qualys_access_token
  scanner_image_uri   = var.scanner_image_uri_map["us-east-1"]

  enable_s3_results         = true
  enable_sns_notifications  = true

  tags = {
    Environment = "production"
    Application = "qualys-lambda-scanner"
  }
}

module "scanner_us_west_2" {
  source = "../../modules/scanner-single-account"
  count  = contains(var.regions, "us-west-2") ? 1 : 0

  providers = {
    aws = aws.us-west-2
  }

  region              = "us-west-2"
  qualys_pod          = var.qualys_pod
  qualys_access_token = var.qualys_access_token
  scanner_image_uri   = var.scanner_image_uri_map["us-west-2"]

  enable_s3_results         = true
  enable_sns_notifications  = true

  tags = {
    Environment = "production"
    Application = "qualys-lambda-scanner"
  }
}

module "scanner_eu_west_1" {
  source = "../../modules/scanner-single-account"
  count  = contains(var.regions, "eu-west-1") ? 1 : 0

  providers = {
    aws = aws.eu-west-1
  }

  region              = "eu-west-1"
  qualys_pod          = var.qualys_pod
  qualys_access_token = var.qualys_access_token
  scanner_image_uri   = var.scanner_image_uri_map["eu-west-1"]

  enable_s3_results         = true
  enable_sns_notifications  = true

  tags = {
    Environment = "production"
    Application = "qualys-lambda-scanner"
  }
}

# Outputs
output "scanner_deployments" {
  description = "Scanner Lambda ARNs by region"
  value = {
    us-east-1 = try(module.scanner_us_east_1[0].scanner_lambda_arn, null)
    us-west-2 = try(module.scanner_us_west_2[0].scanner_lambda_arn, null)
    eu-west-1 = try(module.scanner_eu_west_1[0].scanner_lambda_arn, null)
  }
}

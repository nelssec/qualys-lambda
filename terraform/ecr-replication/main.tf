terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

# This module sets up ECR cross-region replication for the scanner image
# Deploy this in your primary region where you build the Docker image

variable "primary_region" {
  description = "Primary region where images are built and pushed"
  type        = string
  default     = "us-east-1"
}

variable "replication_regions" {
  description = "Regions to replicate images to"
  type        = list(string)
  default     = ["us-west-2", "eu-west-1"]
}

variable "repository_name" {
  description = "ECR repository name"
  type        = string
  default     = "qualys-lambda-scanner"
}

# Primary ECR repository
resource "aws_ecr_repository" "scanner" {
  name                 = var.repository_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name      = var.repository_name
    ManagedBy = "Terraform"
  }
}

# Configure replication
resource "aws_ecr_replication_configuration" "scanner" {
  replication_configuration {
    rule {
      dynamic "destination" {
        for_each = var.replication_regions
        content {
          region      = destination.value
          registry_id = data.aws_caller_identity.current.account_id
        }
      }

      repository_filter {
        filter      = var.repository_name
        filter_type = "PREFIX_MATCH"
      }
    }
  }
}

# Create repositories in replication regions
resource "aws_ecr_repository" "scanner_replicas" {
  for_each = toset(var.replication_regions)

  provider = aws.replica

  name                 = var.repository_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name      = var.repository_name
    ManagedBy = "Terraform"
    Replica   = "true"
  }
}

data "aws_caller_identity" "current" {}

output "primary_repository_url" {
  description = "URL of the primary ECR repository"
  value       = aws_ecr_repository.scanner.repository_url
}

output "replica_repository_urls" {
  description = "URLs of replica ECR repositories"
  value = {
    for region, repo in aws_ecr_repository.scanner_replicas :
    region => repo.repository_url
  }
}

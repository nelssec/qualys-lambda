variable "region" {
  description = "AWS region where the scanner will be deployed"
  type        = string
}

variable "stack_name" {
  description = "CloudFormation stack name. If empty, will be auto-generated"
  type        = string
  default     = ""
}

variable "qualys_pod" {
  description = "Qualys POD (e.g., US1, US2, EU1)"
  type        = string

  validation {
    condition     = contains(["US1", "US2", "US3", "EU1", "EU2", "IN1", "CA1", "AE1"], var.qualys_pod)
    error_message = "Qualys POD must be one of: US1, US2, US3, EU1, EU2, IN1, CA1, AE1"
  }
}

variable "qualys_access_token" {
  description = "Qualys Access Token (stored in Secrets Manager)"
  type        = string
  sensitive   = true
}

variable "scanner_image_uri" {
  description = "ECR URI of the Scanner Lambda container image"
  type        = string
}

variable "enable_s3_results" {
  description = "Create S3 bucket for storing scan results"
  type        = bool
  default     = true
}

variable "enable_sns_notifications" {
  description = "Create SNS topic for scan notifications"
  type        = bool
  default     = true
}

variable "scanner_memory_size" {
  description = "Memory size for Scanner Lambda in MB"
  type        = number
  default     = 2048

  validation {
    condition     = var.scanner_memory_size >= 512 && var.scanner_memory_size <= 10240
    error_message = "Scanner memory size must be between 512 and 10240 MB"
  }
}

variable "scanner_timeout" {
  description = "Timeout for Scanner Lambda in seconds"
  type        = number
  default     = 900

  validation {
    condition     = var.scanner_timeout >= 60 && var.scanner_timeout <= 900
    error_message = "Scanner timeout must be between 60 and 900 seconds"
  }
}

variable "scanner_ephemeral_storage" {
  description = "Ephemeral storage for Scanner Lambda in MB"
  type        = number
  default     = 2048

  validation {
    condition     = var.scanner_ephemeral_storage >= 512 && var.scanner_ephemeral_storage <= 10240
    error_message = "Scanner ephemeral storage must be between 512 and 10240 MB"
  }
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}

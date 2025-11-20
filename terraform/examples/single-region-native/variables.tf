variable "aws_region" {
  description = "AWS region to deploy the scanner"
  type        = string
  default     = "us-east-1"
}

variable "stack_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "qualys-lambda-scanner"
}

variable "qualys_pod" {
  description = "Qualys POD (e.g., US2)"
  type        = string
  default     = "US2"
}

variable "qualys_access_token" {
  description = "Qualys Access Token"
  type        = string
  sensitive   = true
}

variable "qscanner_layer_zip" {
  description = "Path to QScanner Lambda Layer ZIP file"
  type        = string
  default     = "../../../build/qscanner-layer.zip"
}

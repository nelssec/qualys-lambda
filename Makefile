.PHONY: help layer package deploy deploy-multi-region clean

# Variables
AWS_REGION ?= us-east-1
STACK_NAME ?= qscanner
QUALYS_POD ?= US2
LAYER_NAME ?= qscanner
S3_BUCKET ?= $(STACK_NAME)-artifacts-$(shell aws sts get-caller-identity --query Account --output text)
QUALYS_ACCESS_TOKEN ?= $(shell echo $$QUALYS_ACCESS_TOKEN)

help:
	@echo "Qualys Lambda Scanner - Makefile"
	@echo ""
	@echo "Targets:"
	@echo "  layer                 - Build QScanner Lambda Layer"
	@echo "  package              - Package Lambda function code"
	@echo "  deploy               - Deploy scanner to single region"
	@echo "  deploy-multi-region  - Deploy scanner to multiple regions"
	@echo "  clean                - Clean build artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  AWS_REGION           - AWS region (default: us-east-1)"
	@echo "  STACK_NAME           - CloudFormation stack name"
	@echo "  QUALYS_POD           - Qualys POD (default: US2)"
	@echo "  QUALYS_ACCESS_TOKEN  - Qualys access token (required for deploy)"

# Build Lambda Layer with QScanner binary
layer:
	@echo "Building QScanner Lambda Layer..."
	@if [ ! -f scanner-lambda/qscanner ]; then \
		echo "ERROR: qscanner binary not found in scanner-lambda/"; \
		echo "Please download QScanner and place it in scanner-lambda/qscanner"; \
		exit 1; \
	fi
	@mkdir -p build/layer/bin
	@cp scanner-lambda/qscanner build/layer/bin/
	@chmod +x build/layer/bin/qscanner
	@cd build/layer && zip -r ../qscanner-layer.zip .
	@echo "Layer created: build/qscanner-layer.zip"
	@du -h build/qscanner-layer.zip

# Package Lambda function code
package:
	@echo "Packaging Lambda function code..."
	@mkdir -p build/function
	@cp scanner-lambda/lambda_function.py build/function/
	@cd build/function && zip -r ../scanner-function.zip .
	@echo "Function package created: build/scanner-function.zip"

# Publish Lambda Layer to AWS
publish-layer: layer
	@echo "Publishing Lambda Layer to AWS..."
	@aws lambda publish-layer-version \
		--layer-name $(LAYER_NAME) \
		--description "Qualys QScanner binary" \
		--zip-file fileb://build/qscanner-layer.zip \
		--compatible-runtimes python3.11 python3.12 \
		--region $(AWS_REGION) \
		--query 'LayerVersionArn' \
		--output text > build/layer-arn.txt
	@echo "Layer published: $$(cat build/layer-arn.txt)"

# Create S3 bucket for Lambda code if it doesn't exist
create-bucket:
	@echo "Creating S3 bucket for artifacts..."
	@aws s3 mb s3://$(S3_BUCKET) --region $(AWS_REGION) 2>/dev/null || true

# Upload Lambda function code to S3
upload-function: package create-bucket
	@echo "Uploading Lambda function code to S3..."
	@aws s3 cp build/scanner-function.zip s3://$(S3_BUCKET)/scanner-function.zip
	@echo "Function code uploaded to s3://$(S3_BUCKET)/scanner-function.zip"

# Create Secrets Manager secret (done separately for security)
create-secret:
	@echo "Creating Secrets Manager secret..."
	@if [ -z "$(QUALYS_ACCESS_TOKEN)" ]; then \
		echo "ERROR: QUALYS_ACCESS_TOKEN environment variable not set"; \
		exit 1; \
	fi
	@SECRET_ARN=$$(aws secretsmanager create-secret \
		--name "$(STACK_NAME)-qualys-credentials" \
		--description "Qualys credentials for Lambda scanner" \
		--secret-string '{"qualys_pod":"$(QUALYS_POD)","qualys_access_token":"$(QUALYS_ACCESS_TOKEN)"}' \
		--region $(AWS_REGION) \
		--query ARN \
		--output text 2>/dev/null || \
		aws secretsmanager describe-secret \
		--secret-id "$(STACK_NAME)-qualys-credentials" \
		--region $(AWS_REGION) \
		--query ARN \
		--output text); \
	echo $$SECRET_ARN > build/secret-arn.txt
	@echo "Secret ARN: $$(cat build/secret-arn.txt)"

# Deploy stack (native Lambda with Layer)
deploy: publish-layer upload-function create-secret
	@echo "Deploying CloudFormation stack..."
	@aws cloudformation deploy \
		--template-file cloudformation/single-account-native.yaml \
		--stack-name $(STACK_NAME) \
		--parameter-overrides \
			QualysPod=$(QUALYS_POD) \
			QualysSecretArn=$$(cat build/secret-arn.txt) \
			QScannerLayerArn=$$(cat build/layer-arn.txt) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo "Deployment complete!"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query 'Stacks[0].Outputs' \
		--region $(AWS_REGION)

# Update Lambda function code only
update-function: upload-function
	@echo "Updating Lambda function code..."
	@aws lambda update-function-code \
		--function-name $(STACK_NAME)-scanner \
		--s3-bucket $(S3_BUCKET) \
		--s3-key scanner-function.zip \
		--region $(AWS_REGION)
	@echo "Function code updated"

# Deploy to multiple regions
deploy-multi-region:
	@echo "Deploying to multiple regions..."
	@for region in us-east-1 us-west-2 eu-west-1; do \
		echo "Deploying to $$region..."; \
		$(MAKE) deploy AWS_REGION=$$region STACK_NAME=$(STACK_NAME)-$$region; \
	done

# Clean build artifacts
clean:
	@echo "Cleaning build artifacts..."
	@rm -rf build/
	@echo "Clean complete"

# Delete stack
delete:
	@echo "Deleting CloudFormation stack..."
	@aws cloudformation delete-stack \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION)
	@echo "Stack deletion initiated. Waiting for completion..."
	@aws cloudformation wait stack-delete-complete \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION)
	@echo "Stack deleted"

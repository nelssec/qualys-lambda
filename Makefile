.PHONY: help layer package deploy deploy-multi-region clean deploy-stackset deploy-hub deploy-spoke

# Variables
AWS_REGION ?= us-east-1
STACK_NAME ?= qualys-lambda-scanner
QUALYS_POD ?= US2
LAYER_NAME ?= qscanner
S3_BUCKET ?= $(STACK_NAME)-artifacts-$(shell aws sts get-caller-identity --query Account --output text)
QUALYS_ACCESS_TOKEN ?= $(shell echo $$QUALYS_ACCESS_TOKEN)

# Qualys tagging variables (optional)
TAG ?= false
USERNAME ?=
PASSWORD ?=

# Cross-account security
EXTERNAL_ID ?= $(shell openssl rand -hex 16)

# StackSet/Organization variables
ORG_ID ?= $(shell aws organizations describe-organization --query 'Organization.Id' --output text 2>/dev/null)
ORG_UNIT_IDS ?=
ADMIN_ACCOUNT_ID ?= $(shell aws sts get-caller-identity --query Account --output text)

help:
	@echo "Qualys Lambda Scanner - Makefile"
	@echo ""
	@echo "=== Single Account Deployment ==="
	@echo "  deploy               - Deploy scanner to single region"
	@echo "  deploy-multi-region  - Deploy scanner to multiple regions"
	@echo "  update-function      - Update Lambda function code only"
	@echo ""
	@echo "=== Multi-Account StackSet Deployment ==="
	@echo "  deploy-stackset      - Deploy StackSet to organization OUs"
	@echo "  delete-stackset      - Delete StackSet from organization"
	@echo ""
	@echo "=== Centralized Hub-Spoke Deployment ==="
	@echo "  deploy-hub           - Deploy hub scanner in security account"
	@echo "  deploy-spoke-stackset - Deploy spoke template via StackSet"
	@echo "  delete-hub           - Delete hub stack"
	@echo "  delete-spoke-stackset - Delete spoke StackSet"
	@echo ""
	@echo "=== Build & Utilities ==="
	@echo "  layer                - Build QScanner Lambda Layer"
	@echo "  package              - Package Lambda function code"
	@echo "  clean                - Clean build artifacts"
	@echo "  delete               - Delete single-account stack"
	@echo ""
	@echo "Variables:"
	@echo "  AWS_REGION           - AWS region (default: us-east-1)"
	@echo "  STACK_NAME           - CloudFormation stack name (default: qscanner)"
	@echo "  QUALYS_POD           - Qualys POD (default: US2)"
	@echo "  QUALYS_ACCESS_TOKEN  - Qualys access token (required, or set env var)"
	@echo "  ORG_UNIT_IDS         - Comma-separated OU IDs for StackSet deployment"
	@echo "  TAG                  - Enable Qualys image tagging (true/false, default: false)"
	@echo "  USERNAME             - Qualys API username (required if TAG=true)"
	@echo "  PASSWORD             - Qualys API password (required if TAG=true)"
	@echo ""
	@echo "Examples:"
	@echo "  make deploy QUALYS_POD=US2 AWS_REGION=us-east-1"
	@echo "  make deploy TAG=true USERNAME=myuser PASSWORD=mypass"
	@echo "  make deploy-hub TAG=true USERNAME=myuser PASSWORD=mypass"
	@echo "  make deploy-stackset ORG_UNIT_IDS=ou-xxxx TAG=true USERNAME=myuser PASSWORD=mypass"

# =============================================================================
# Build Targets
# =============================================================================

# Build Lambda Layer with QScanner binary
layer:
	@echo "Building QScanner Lambda Layer..."
	@if [ ! -f scanner-lambda/qscanner.gz ]; then \
		echo "ERROR: qscanner.gz not found in scanner-lambda/"; \
		echo "Please download QScanner and place it in scanner-lambda/qscanner.gz"; \
		exit 1; \
	fi
	@mkdir -p build/layer/bin
	@gunzip -c scanner-lambda/qscanner.gz > build/layer/bin/qscanner
	@chmod +x build/layer/bin/qscanner
	@cd build/layer && zip -r ../qscanner-layer.zip .
	@echo "Layer created: build/qscanner-layer.zip"
	@du -h build/qscanner-layer.zip

# Package Lambda function code
package:
	@echo "Packaging Lambda function code..."
	@mkdir -p build/function build/bulk-scan
	@cp scanner-lambda/lambda_function.py build/function/
	@cp scanner-lambda/bulk_scan.py build/bulk-scan/
	@cd build/function && zip -r ../scanner-function.zip .
	@cd build/bulk-scan && zip -r ../bulk-scan.zip .
	@echo "Function packages created: build/scanner-function.zip, build/bulk-scan.zip"

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

# Create S3 bucket for Lambda artifacts
create-bucket:
	@echo "Creating S3 bucket for artifacts..."
	@aws s3 mb s3://$(S3_BUCKET) --region $(AWS_REGION) 2>/dev/null || true

# Upload Lambda function code to S3
upload-function: package create-bucket
	@echo "Uploading Lambda function code to S3..."
	@aws s3 cp build/scanner-function.zip s3://$(S3_BUCKET)/scanner-function.zip
	@echo "Function code uploaded to s3://$(S3_BUCKET)/scanner-function.zip"

# Create Secrets Manager secret
create-secret:
	@echo "Creating Secrets Manager secret..."
	@if [ -z "$(QUALYS_ACCESS_TOKEN)" ]; then \
		echo "ERROR: QUALYS_ACCESS_TOKEN environment variable not set"; \
		exit 1; \
	fi
	@mkdir -p build
	@if [ -n "$(USERNAME)" ] && [ -n "$(PASSWORD)" ]; then \
		SECRET_JSON='{"qualys_pod":"$(QUALYS_POD)","qualys_access_token":"$(QUALYS_ACCESS_TOKEN)","qualys_api_username":"$(USERNAME)","qualys_api_password":"$(PASSWORD)"}'; \
	else \
		SECRET_JSON='{"qualys_pod":"$(QUALYS_POD)","qualys_access_token":"$(QUALYS_ACCESS_TOKEN)"}'; \
	fi; \
	SECRET_ARN=$$(aws secretsmanager create-secret \
		--name "$(STACK_NAME)-qualys-credentials" \
		--description "Qualys credentials for Lambda scanner" \
		--secret-string "$$SECRET_JSON" \
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

# =============================================================================
# Single Account Deployment
# =============================================================================

# Deploy to single account/region
deploy: publish-layer upload-function create-secret
	@echo "Deploying CloudFormation stack..."
	@if [ "$(TAG)" = "true" ] && { [ -z "$(USERNAME)" ] || [ -z "$(PASSWORD)" ]; }; then \
		echo "ERROR: TAG=true requires USERNAME and PASSWORD"; \
		echo "Usage: make deploy TAG=true USERNAME=myuser PASSWORD=mypass"; \
		exit 1; \
	fi
	@aws cloudformation deploy \
		--template-file cloudformation/single-account-native.yaml \
		--stack-name $(STACK_NAME) \
		--parameter-overrides \
			QualysPod=$(QUALYS_POD) \
			QualysSecretArn=$$(cat build/secret-arn.txt) \
			QScannerLayerArn=$$(cat build/layer-arn.txt) \
			EnableQualysTagging=$(TAG) \
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

# Delete single-account stack
delete:
	@echo "Deleting CloudFormation stack..."
	@aws cloudformation delete-stack \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION)
	@echo "Waiting for stack deletion..."
	@aws cloudformation wait stack-delete-complete \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION)
	@echo "Stack deleted"

# =============================================================================
# Multi-Account StackSet Deployment
# =============================================================================

# Create S3 bucket with org-wide read access for artifact distribution
create-artifacts-bucket:
	@echo "Creating artifacts bucket for cross-account distribution..."
	@mkdir -p build
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	BUCKET_NAME=qualys-scanner-artifacts-$$ACCOUNT_ID; \
	aws s3 mb s3://$$BUCKET_NAME --region $(AWS_REGION) 2>/dev/null || true; \
	if [ -n "$(ORG_ID)" ] && [ "$(ORG_ID)" != "None" ]; then \
		echo "Applying org-wide bucket policy for $(ORG_ID)..."; \
		aws s3api put-bucket-policy --bucket $$BUCKET_NAME --policy '{"Version":"2012-10-17","Statement":[{"Sid":"AllowOrgAccess","Effect":"Allow","Principal":"*","Action":["s3:GetObject","s3:GetObjectVersion"],"Resource":"arn:aws:s3:::'$$BUCKET_NAME'/*","Condition":{"StringEquals":{"aws:PrincipalOrgID":"$(ORG_ID)"}}}]}'; \
	else \
		echo "No ORG_ID provided - skipping org-wide bucket policy (single account mode)"; \
	fi; \
	echo $$BUCKET_NAME > build/artifacts-bucket.txt
	@echo "Artifacts bucket: $$(cat build/artifacts-bucket.txt)"

# Upload Lambda artifacts to S3 for cross-account access
upload-artifacts: layer package create-artifacts-bucket
	@echo "Uploading artifacts to S3..."
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws s3 cp build/qscanner-layer.zip s3://$$BUCKET/qualys-lambda-scanner/qscanner-layer.zip; \
	aws s3 cp build/scanner-function.zip s3://$$BUCKET/qualys-lambda-scanner/lambda-code.zip; \
	aws s3 cp build/bulk-scan.zip s3://$$BUCKET/qualys-lambda-scanner/bulk-scan.zip
	@echo "Artifacts uploaded to s3://$$BUCKET/qualys-lambda-scanner/"

# Deploy StackSet to organization (each account gets own scanner)
deploy-stackset: upload-artifacts
	@echo "Deploying StackSet to organization..."
	@if [ -z "$(QUALYS_ACCESS_TOKEN)" ]; then \
		echo "ERROR: QUALYS_ACCESS_TOKEN environment variable not set"; \
		exit 1; \
	fi
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS not set."; \
		echo "Usage: make deploy-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx"; \
		exit 1; \
	fi
	@if [ "$(TAG)" = "true" ] && { [ -z "$(USERNAME)" ] || [ -z "$(PASSWORD)" ]; }; then \
		echo "ERROR: TAG=true requires USERNAME and PASSWORD"; \
		echo "Usage: make deploy-stackset ORG_UNIT_IDS=ou-xxxx TAG=true USERNAME=myuser PASSWORD=mypass"; \
		exit 1; \
	fi
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws cloudformation create-stack-set \
		--stack-set-name $(STACK_NAME)-stackset \
		--template-body file://cloudformation/stackset.yaml \
		--parameters \
			ParameterKey=QualysPod,ParameterValue=$(QUALYS_POD) \
			ParameterKey=QualysAccessToken,ParameterValue=$(QUALYS_ACCESS_TOKEN) \
			ParameterKey=ArtifactsBucket,ParameterValue=$$BUCKET \
			ParameterKey=EnableQualysTagging,ParameterValue=$(TAG) \
			ParameterKey=QualysApiUsername,ParameterValue=$(USERNAME) \
			ParameterKey=QualysApiPassword,ParameterValue=$(PASSWORD) \
		--capabilities CAPABILITY_NAMED_IAM \
		--permission-model SERVICE_MANAGED \
		--auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
		--region $(AWS_REGION) 2>/dev/null || \
		aws cloudformation update-stack-set \
			--stack-set-name $(STACK_NAME)-stackset \
			--template-body file://cloudformation/stackset.yaml \
			--parameters \
				ParameterKey=QualysPod,ParameterValue=$(QUALYS_POD) \
				ParameterKey=QualysAccessToken,ParameterValue=$(QUALYS_ACCESS_TOKEN) \
				ParameterKey=ArtifactsBucket,ParameterValue=$$BUCKET \
				ParameterKey=EnableQualysTagging,ParameterValue=$(TAG) \
				ParameterKey=QualysApiUsername,ParameterValue=$(USERNAME) \
				ParameterKey=QualysApiPassword,ParameterValue=$(PASSWORD) \
			--capabilities CAPABILITY_NAMED_IAM \
			--region $(AWS_REGION)
	@echo "Creating stack instances in OUs: $(ORG_UNIT_IDS)..."
	@aws cloudformation create-stack-instances \
		--stack-set-name $(STACK_NAME)-stackset \
		--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
		--regions $(AWS_REGION) \
		--operation-preferences FailureTolerancePercentage=10,MaxConcurrentPercentage=25 \
		--region $(AWS_REGION)
	@echo ""
	@echo "StackSet deployment initiated!"
	@echo "Monitor: aws cloudformation list-stack-instances --stack-set-name $(STACK_NAME)-stackset --region $(AWS_REGION)"

# Delete StackSet
delete-stackset:
	@echo "Deleting StackSet instances..."
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS required to delete instances"; \
		exit 1; \
	fi
	@aws cloudformation delete-stack-instances \
		--stack-set-name $(STACK_NAME)-stackset \
		--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
		--regions $(AWS_REGION) \
		--no-retain-stacks \
		--region $(AWS_REGION) || true
	@echo "Waiting for instances to be deleted (60s)..."
	@sleep 60
	@aws cloudformation delete-stack-set \
		--stack-set-name $(STACK_NAME)-stackset \
		--region $(AWS_REGION)
	@echo "StackSet deleted"

# =============================================================================
# Centralized Hub-Spoke Deployment
# =============================================================================

# Deploy hub scanner in security/central account
deploy-hub: upload-artifacts
	@echo "Deploying centralized hub scanner..."
	@if [ -z "$(QUALYS_ACCESS_TOKEN)" ]; then \
		echo "ERROR: QUALYS_ACCESS_TOKEN environment variable not set"; \
		exit 1; \
	fi
	@if [ "$(TAG)" = "true" ]; then \
		if [ -z "$(USERNAME)" ] || [ -z "$(PASSWORD)" ]; then \
			echo "ERROR: TAG=true requires USERNAME and PASSWORD"; \
			echo "Usage: make deploy-hub TAG=true USERNAME=myuser PASSWORD=mypass"; \
			exit 1; \
		fi; \
	fi
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws cloudformation deploy \
		--template-file cloudformation/centralized-hub.yaml \
		--stack-name $(STACK_NAME)-hub \
		--parameter-overrides \
			QualysPod=$(QUALYS_POD) \
			QualysAccessToken=$(QUALYS_ACCESS_TOKEN) \
			ArtifactsBucket=$$BUCKET \
			OrganizationId=$(ORG_ID) \
			ScannerExternalId=$(EXTERNAL_ID) \
			EnableQualysTagging=$(TAG) \
			QualysApiUsername=$(USERNAME) \
			QualysApiPassword=$(PASSWORD) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo ""
	@echo "Hub deployment complete!"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME)-hub \
		--query 'Stacks[0].Outputs' \
		--region $(AWS_REGION) \
		--output table
	@# Save outputs for spoke deployment
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME)-hub \
		--query "Stacks[0].Outputs[?OutputKey=='CentralEventBusArn'].OutputValue" \
		--output text \
		--region $(AWS_REGION) > build/central-bus-arn.txt
	@echo ""
	@echo "Next: make deploy-spoke-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx"

# Deploy spoke template via StackSet to member accounts
deploy-spoke-stackset:
	@echo "Deploying spoke StackSet to member accounts..."
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS required"; \
		exit 1; \
	fi
	@if [ ! -f build/central-bus-arn.txt ]; then \
		echo "ERROR: Deploy hub first: make deploy-hub"; \
		exit 1; \
	fi
	@SECURITY_ACCT=$$(aws sts get-caller-identity --query Account --output text); \
	CENTRAL_BUS_ARN=$$(cat build/central-bus-arn.txt); \
	CENTRAL_BUS_NAME=$$(echo $$CENTRAL_BUS_ARN | awk -F'/' '{print $$NF}'); \
	aws cloudformation create-stack-set \
		--stack-set-name $(STACK_NAME)-spoke-stackset \
		--template-body file://cloudformation/centralized-spoke.yaml \
		--parameters \
			ParameterKey=SecurityAccountId,ParameterValue=$$SECURITY_ACCT \
			ParameterKey=CentralEventBusName,ParameterValue=$$CENTRAL_BUS_NAME \
			ParameterKey=CentralEventBusArn,ParameterValue=$$CENTRAL_BUS_ARN \
		--capabilities CAPABILITY_NAMED_IAM \
		--permission-model SERVICE_MANAGED \
		--auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
		--region $(AWS_REGION) 2>/dev/null || \
		aws cloudformation update-stack-set \
			--stack-set-name $(STACK_NAME)-spoke-stackset \
			--template-body file://cloudformation/centralized-spoke.yaml \
			--parameters \
				ParameterKey=SecurityAccountId,ParameterValue=$$SECURITY_ACCT \
				ParameterKey=CentralEventBusName,ParameterValue=$$CENTRAL_BUS_NAME \
				ParameterKey=CentralEventBusArn,ParameterValue=$$CENTRAL_BUS_ARN \
			--capabilities CAPABILITY_NAMED_IAM \
			--region $(AWS_REGION)
	@echo "Creating spoke instances in OUs: $(ORG_UNIT_IDS)..."
	@aws cloudformation create-stack-instances \
		--stack-set-name $(STACK_NAME)-spoke-stackset \
		--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
		--regions $(AWS_REGION) \
		--operation-preferences FailureTolerancePercentage=10,MaxConcurrentPercentage=25 \
		--region $(AWS_REGION)
	@echo ""
	@echo "Spoke StackSet deployment initiated!"

# Delete spoke StackSet
delete-spoke-stackset:
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS required"; \
		exit 1; \
	fi
	@aws cloudformation delete-stack-instances \
		--stack-set-name $(STACK_NAME)-spoke-stackset \
		--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
		--regions $(AWS_REGION) \
		--no-retain-stacks \
		--region $(AWS_REGION) || true
	@sleep 60
	@aws cloudformation delete-stack-set \
		--stack-set-name $(STACK_NAME)-spoke-stackset \
		--region $(AWS_REGION)
	@echo "Spoke StackSet deleted"

# Delete hub
delete-hub:
	@aws cloudformation delete-stack \
		--stack-name $(STACK_NAME)-hub \
		--region $(AWS_REGION)
	@aws cloudformation wait stack-delete-complete \
		--stack-name $(STACK_NAME)-hub \
		--region $(AWS_REGION)
	@echo "Hub deleted"

# =============================================================================
# Utilities
# =============================================================================

# Clean build artifacts
clean:
	@rm -rf build/
	@echo "Build artifacts cleaned"

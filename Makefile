.PHONY: help update-qscanner layer package deploy quickstart deploy-multi-region clean deploy-stackset deploy-hub deploy-spoke \
       deploy-spoke-minimal deploy-spoke-minimal-stackset deploy-org-forwarder \
       delete delete-hub delete-stackset delete-spoke-stackset delete-org-forwarder \
       delete-bucket delete-buckets delete-artifacts-bucket delete-secret delete-layers \
       delete-dynamodb delete-dlq delete-sns delete-log-groups delete-alarms delete-eventbridge-rules delete-kms-key \
       clean-all clean-all-hub clean-all-stackset clean-dry-run clean-all-resources \
       test test-unit test-integration test-smoke test-bulk-dry-run test-coverage \
       validate validate-config validate-cfn config-init config-show install-dev

AWS_REGION ?= us-east-1
STACK_NAME ?= qualys-lambda-scanner
QUALYS_POD ?= US2
LAYER_NAME ?= qscanner
S3_BUCKET ?= $(STACK_NAME)-artifacts-$(shell aws sts get-caller-identity --query Account --output text)
QUALYS_ACCESS_TOKEN ?= $(shell echo $$QUALYS_ACCESS_TOKEN)

TAG ?= true

# Multi-region support - comma-separated list of regions
REGIONS ?= $(AWS_REGION)

EXTERNAL_ID ?= $(shell openssl rand -hex 16)

ORG_ID ?= $(shell aws organizations describe-organization --query 'Organization.Id' --output text 2>/dev/null)
ORG_UNIT_IDS ?=
ADMIN_ACCOUNT_ID ?= $(shell aws sts get-caller-identity --query Account --output text)

help:
	@echo "Qualys Lambda Scanner - Makefile"
	@echo ""
	@echo "=== Single Account Deployment ==="
	@echo "  quickstart           - One-command deploy (creates secret + layer in CFN)"
	@echo "  deploy               - Deploy scanner to single region"
	@echo "  deploy-multi-region  - Deploy scanner to multiple regions"
	@echo "  update-function      - Update Lambda function code only"
	@echo "  delete               - Delete single-account CloudFormation stack"
	@echo ""
	@echo "=== Multi-Account StackSet Deployment ==="
	@echo "  deploy-stackset      - Deploy StackSet to organization OUs"
	@echo "  delete-stackset      - Delete StackSet from organization"
	@echo ""
	@echo "=== Centralized Hub-Spoke Deployment ==="
	@echo "  deploy-hub           - Deploy hub scanner in security account"
	@echo "  deploy-spoke-stackset - Deploy spoke template via StackSet (creates CloudTrail)"
	@echo "  delete-hub           - Delete hub stack"
	@echo "  delete-spoke-stackset - Delete spoke StackSet"
	@echo ""
	@echo "=== Hub-Spoke with Org CloudTrail (no new CloudTrails) ==="
	@echo "  deploy-org-forwarder - Deploy EventBridge forwarder in management account"
	@echo "  deploy-spoke-minimal - Deploy minimal spoke (IAM role only) to single account"
	@echo "  deploy-spoke-minimal-stackset - Deploy minimal spoke via StackSet"
	@echo "  delete-org-forwarder - Delete org forwarder stack"
	@echo ""
	@echo "=== Build ==="
	@echo "  update-qscanner      - Update qscanner.gz from downloaded Linux binary"
	@echo "  layer                - Build QScanner Lambda Layer"
	@echo "  package              - Package Lambda function code"
	@echo ""
	@echo "=== Cleanup (IMPORTANT) ==="
	@echo "  clean                - Clean local build artifacts only"
	@echo "  clean-dry-run        - Show what AWS resources would be deleted"
	@echo "  clean-all            - FULL cleanup: stack, buckets, secret, layers (single-account)"
	@echo "  clean-all-hub        - FULL cleanup for hub-spoke deployment"
	@echo "  clean-all-stackset   - FULL cleanup for StackSet deployment"
	@echo ""
	@echo "=== Individual Resource Cleanup ==="
	@echo "  delete-buckets       - Delete all S3 buckets for this stack"
	@echo "  delete-secret        - Delete Secrets Manager secret"
	@echo "  delete-layers        - Delete all Lambda layer versions"
	@echo "  delete-bucket        - Delete specific bucket (BUCKET_NAME=xxx)"
	@echo "  delete-artifacts-bucket - Delete cross-account artifacts bucket"
	@echo "  delete-dynamodb      - Delete DynamoDB scan cache table"
	@echo "  delete-dlq           - Delete SQS Dead Letter Queue"
	@echo "  delete-sns           - Delete SNS notification topic"
	@echo "  delete-log-groups    - Delete CloudWatch Log Groups"
	@echo "  delete-alarms        - Delete CloudWatch Alarms"
	@echo "  delete-eventbridge-rules - Delete EventBridge Rules"
	@echo "  delete-kms-key       - Schedule KMS key for deletion"
	@echo ""
	@echo "Variables:"
	@echo "  AWS_REGION              - AWS region (default: us-east-1)"
	@echo "  STACK_NAME              - CloudFormation stack name (default: qualys-lambda-scanner)"
	@echo "  QUALYS_POD              - Qualys POD (default: US2)"
	@echo "  QUALYS_ACCESS_TOKEN     - Qualys access token (required, or set env var)"
	@echo "  ORG_UNIT_IDS            - Comma-separated OU IDs for StackSet deployment"
	@echo "  TAG                     - Enable Lambda resource tagging (true/false, default: true)"
	@echo "  LAYER_NAME              - Lambda layer name (default: qscanner)"
	@echo "  QSCANNER_BINARIES_DIR   - Directory with downloaded qscanner binaries"
	@echo "                            (default: ~/git_base/infra/binaries)"
	@echo "  HUB_ACCOUNT_ID          - Hub account ID (for org forwarder and minimal spoke)"
	@echo "  EXTERNAL_ID             - External ID for cross-account role (auto-generated if not set)"
	@echo "  REGIONS                 - Comma-separated regions for multi-region StackSet"
	@echo ""
	@echo "Examples:"
	@echo "  make deploy QUALYS_POD=US2 AWS_REGION=us-east-1"
	@echo "  make deploy TAG=false  # Disable Lambda tagging"
	@echo "  make deploy-hub"
	@echo "  make deploy-stackset ORG_UNIT_IDS=ou-xxxx"
	@echo ""
	@echo "=== Testing ==="
	@echo "  test               - Run all unit tests (no AWS required)"
	@echo "  test-unit          - Run unit tests only"
	@echo "  test-integration   - Run integration tests (requires AWS)"
	@echo "  test-smoke         - Smoke test with real Lambda deployment"
	@echo "  test-bulk-dry-run  - Test bulk scan in dry-run mode"
	@echo "  test-coverage      - Run tests with coverage report"
	@echo ""
	@echo "=== Validation ==="
	@echo "  validate           - Run pre-flight validation before deploy"
	@echo "  validate-cfn       - Lint CloudFormation templates"
	@echo ""
	@echo "=== Configuration ==="
	@echo "  config-init        - Create .qualys-scanner.yml from example"
	@echo "  config-show        - Display current configuration"
	@echo "  install-dev        - Install development dependencies"
	@echo ""
	@echo "Cleanup Examples:"
	@echo "  make clean-dry-run                    # Preview what will be deleted"
	@echo "  make clean-all                        # Full cleanup (single-account)"
	@echo "  make clean-all-hub ORG_UNIT_IDS=ou-xxxx  # Full hub-spoke cleanup"
	@echo "  make delete-bucket BUCKET_NAME=my-bucket # Delete specific bucket"
	@echo ""
	@echo "Testing Examples:"
	@echo "  make test                             # Run all unit tests"
	@echo "  make validate                         # Pre-flight checks"
	@echo "  make deploy-stackset REGIONS=us-east-1,us-west-2 ORG_UNIT_IDS=ou-xxx"


QSCANNER_BINARIES_DIR ?= $(HOME)/git_base/infra/binaries

update-qscanner:
	@echo "Updating QScanner binary for Lambda..."
	@TARBALL=$$(ls -t $(QSCANNER_BINARIES_DIR)/qscanner-*.linux-amd64.tar.gz 2>/dev/null | head -1); \
	if [ -z "$$TARBALL" ]; then \
		echo "ERROR: No Linux binary found in $(QSCANNER_BINARIES_DIR)"; \
		echo ""; \
		echo "To download the latest QScanner:"; \
		echo "  1. Run on a Linux system: qscanner update --destination $(QSCANNER_BINARIES_DIR)"; \
		echo "  2. Or download manually from Qualys and place in $(QSCANNER_BINARIES_DIR)"; \
		echo ""; \
		echo "Expected filename pattern: qscanner-*.linux-amd64.tar.gz"; \
		exit 1; \
	fi; \
	echo "Found: $$TARBALL"; \
	VERSION=$$(basename "$$TARBALL" | sed 's/qscanner-\(.*\)\.linux-amd64\.tar\.gz/\1/'); \
	echo "Version: $$VERSION"; \
	tar -xzf "$$TARBALL" -C /tmp qscanner; \
	gzip -c /tmp/qscanner > scanner-lambda/qscanner.gz; \
	rm /tmp/qscanner; \
	echo ""; \
	echo "Updated scanner-lambda/qscanner.gz ($$VERSION)"; \
	ls -lh scanner-lambda/qscanner.gz; \
	echo ""; \
	echo "Next steps:"; \
	echo "  make layer          # Build the layer zip"; \
	echo "  make publish-layer  # Publish to AWS"; \
	echo "  make deploy         # Full redeploy"

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

package:
	@echo "Packaging Lambda function code..."
	@mkdir -p build/function build/bulk-scan
	@cp scanner-lambda/lambda_function.py build/function/
	@cp scanner-lambda/bulk_scan.py build/bulk-scan/
	@cd build/function && zip -r ../scanner-function.zip .
	@cd build/bulk-scan && zip -r ../bulk-scan.zip .
	@echo "Function packages created: build/scanner-function.zip, build/bulk-scan.zip"

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

create-bucket:
	@echo "Creating S3 bucket for artifacts..."
	@aws s3 mb s3://$(S3_BUCKET) --region $(AWS_REGION) 2>/dev/null || true

upload-function: package create-bucket
	@echo "Uploading Lambda function code to S3..."
	@aws s3 cp build/scanner-function.zip s3://$(S3_BUCKET)/scanner-function.zip
	@aws s3 cp build/bulk-scan.zip s3://$(S3_BUCKET)/bulk-scan.zip
	@echo "Function code uploaded to s3://$(S3_BUCKET)/"

create-secret:
	@echo "Creating Secrets Manager secret..."
	@if [ -z "$(QUALYS_ACCESS_TOKEN)" ]; then \
		echo "ERROR: QUALYS_ACCESS_TOKEN environment variable not set"; \
		exit 1; \
	fi
	@mkdir -p build
	@SECRET_JSON='{"qualys_pod":"$(QUALYS_POD)","qualys_access_token":"$(QUALYS_ACCESS_TOKEN)"}'; \
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


deploy: publish-layer upload-function create-secret
	@echo "Deploying CloudFormation stack..."
	@aws cloudformation deploy \
		--template-file cloudformation/single-account-native.yaml \
		--stack-name $(STACK_NAME) \
		--parameter-overrides \
			QualysSecretArn=$$(cat build/secret-arn.txt) \
			QScannerLayerArn=$$(cat build/layer-arn.txt) \
			LambdaCodeBucket=$(S3_BUCKET) \
			LambdaCodeKey=scanner-function.zip \
			BulkScanCodeKey=bulk-scan.zip \
			EnableTagging=$(TAG) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo "Deployment complete!"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query 'Stacks[0].Outputs' \
		--region $(AWS_REGION)

quickstart: layer package create-bucket
	@# Upload all artifacts to S3
	@aws s3 cp build/qscanner-layer.zip s3://$(S3_BUCKET)/qscanner-layer.zip
	@aws s3 cp build/scanner-function.zip s3://$(S3_BUCKET)/scanner-function.zip
	@aws s3 cp build/bulk-scan.zip s3://$(S3_BUCKET)/bulk-scan.zip
	@# Deploy CFN (secret + layer created inline)
	@aws cloudformation deploy \
		--template-file cloudformation/single-account-native.yaml \
		--stack-name $(STACK_NAME) \
		--parameter-overrides \
			QualysPod=$(QUALYS_POD) \
			QualysAccessToken=$(QUALYS_ACCESS_TOKEN) \
			LambdaCodeBucket=$(S3_BUCKET) \
			LambdaCodeKey=scanner-function.zip \
			BulkScanCodeKey=bulk-scan.zip \
			QScannerLayerKey=qscanner-layer.zip \
			EnableBulkScan=true \
			EnableTagging=$(TAG) \
			BulkScanSchedule='cron(0 2 * * ? *)' \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo "Deployment complete!"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query 'Stacks[0].Outputs' \
		--region $(AWS_REGION)

update-function: upload-function
	@echo "Updating Lambda function code..."
	@aws lambda update-function-code \
		--function-name $(STACK_NAME)-scanner \
		--s3-bucket $(S3_BUCKET) \
		--s3-key scanner-function.zip \
		--region $(AWS_REGION)
	@echo "Function code updated"

deploy-multi-region:
	@echo "Deploying to multiple regions..."
	@for region in us-east-1 us-west-2 eu-west-1; do \
		echo "Deploying to $$region..."; \
		$(MAKE) deploy AWS_REGION=$$region STACK_NAME=$(STACK_NAME)-$$region; \
	done

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

upload-artifacts: layer package create-artifacts-bucket
	@echo "Uploading artifacts to S3..."
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws s3 cp build/qscanner-layer.zip s3://$$BUCKET/qualys-lambda-scanner/qscanner-layer.zip; \
	aws s3 cp build/scanner-function.zip s3://$$BUCKET/qualys-lambda-scanner/lambda-code.zip; \
	aws s3 cp build/bulk-scan.zip s3://$$BUCKET/qualys-lambda-scanner/bulk-scan.zip
	@echo "Artifacts uploaded to s3://$$BUCKET/qualys-lambda-scanner/"

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
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws cloudformation create-stack-set \
		--stack-set-name $(STACK_NAME)-stackset \
		--template-body file://cloudformation/stackset.yaml \
		--parameters \
			ParameterKey=QualysPod,ParameterValue=$(QUALYS_POD) \
			ParameterKey=QualysAccessToken,ParameterValue=$(QUALYS_ACCESS_TOKEN) \
			ParameterKey=ArtifactsBucket,ParameterValue=$$BUCKET \
			ParameterKey=EnableTagging,ParameterValue=$(TAG) \
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
				ParameterKey=EnableTagging,ParameterValue=$(TAG) \
			--capabilities CAPABILITY_NAMED_IAM \
			--region $(AWS_REGION)
	@echo "Creating stack instances in OUs: $(ORG_UNIT_IDS)..."
	@echo "Regions: $(REGIONS)"
	@for region in $$(echo "$(REGIONS)" | tr ',' ' '); do \
		echo "Creating instances in $$region..."; \
		aws cloudformation create-stack-instances \
			--stack-set-name $(STACK_NAME)-stackset \
			--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
			--regions $$region \
			--operation-preferences FailureTolerancePercentage=10,MaxConcurrentPercentage=25 \
			--region $(AWS_REGION) || true; \
	done
	@echo ""
	@echo "StackSet deployment initiated!"
	@echo "Monitor: aws cloudformation list-stack-instances --stack-set-name $(STACK_NAME)-stackset --region $(AWS_REGION)"

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


deploy-hub: upload-artifacts create-secret
	@echo "Deploying centralized hub scanner..."
	@BUCKET=$$(cat build/artifacts-bucket.txt); \
	aws cloudformation deploy \
		--template-file cloudformation/centralized-hub.yaml \
		--stack-name $(STACK_NAME)-hub \
		--parameter-overrides \
			QualysSecretArn=$$(cat build/secret-arn.txt) \
			ArtifactsBucket=$$BUCKET \
			OrganizationId=$(ORG_ID) \
			ScannerExternalId=$(EXTERNAL_ID) \
			EnableTagging=$(TAG) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo ""
	@echo "Hub deployment complete!"
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME)-hub \
		--query 'Stacks[0].Outputs' \
		--region $(AWS_REGION) \
		--output table
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME)-hub \
		--query "Stacks[0].Outputs[?OutputKey=='CentralEventBusArn'].OutputValue" \
		--output text \
		--region $(AWS_REGION) > build/central-bus-arn.txt
	@echo ""
	@echo "Next: make deploy-spoke-stackset ORG_UNIT_IDS=ou-xxxx-xxxxxxxx"

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


HUB_ACCOUNT_ID ?=
HUB_EVENT_BUS_NAME ?= $(STACK_NAME)-hub-central-bus

deploy-org-forwarder:
	@echo "Deploying org CloudTrail EventBridge forwarder..."
	@if [ -z "$(HUB_ACCOUNT_ID)" ]; then \
		echo "ERROR: HUB_ACCOUNT_ID required"; \
		echo "Usage: make deploy-org-forwarder HUB_ACCOUNT_ID=123456789012"; \
		exit 1; \
	fi
	aws cloudformation deploy \
		--template-file cloudformation/org-cloudtrail-forwarder.yaml \
		--stack-name $(STACK_NAME)-org-forwarder \
		--parameter-overrides \
			HubAccountId=$(HUB_ACCOUNT_ID) \
			HubEventBusName=$(HUB_EVENT_BUS_NAME) \
			HubRegion=$(AWS_REGION) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo ""
	@echo "Org forwarder deployed! Lambda events will now forward to hub."

delete-org-forwarder:
	@aws cloudformation delete-stack \
		--stack-name $(STACK_NAME)-org-forwarder \
		--region $(AWS_REGION)
	@aws cloudformation wait stack-delete-complete \
		--stack-name $(STACK_NAME)-org-forwarder \
		--region $(AWS_REGION)
	@echo "Org forwarder deleted"

deploy-spoke-minimal:
	@echo "Deploying minimal spoke (IAM role only, no CloudTrail)..."
	@if [ -z "$(HUB_ACCOUNT_ID)" ]; then \
		echo "ERROR: HUB_ACCOUNT_ID required"; \
		echo "Usage: make deploy-spoke-minimal HUB_ACCOUNT_ID=123456789012 EXTERNAL_ID=your-id"; \
		exit 1; \
	fi
	aws cloudformation deploy \
		--template-file cloudformation/centralized-spoke-minimal.yaml \
		--stack-name $(STACK_NAME)-spoke \
		--parameter-overrides \
			SecurityAccountId=$(HUB_ACCOUNT_ID) \
			ScannerExternalId=$(EXTERNAL_ID) \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(AWS_REGION)
	@echo ""
	@echo "Minimal spoke deployed!"

deploy-spoke-minimal-stackset:
	@echo "Deploying minimal spoke StackSet (IAM role only, no CloudTrail)..."
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS required"; \
		exit 1; \
	fi
	@if [ -z "$(HUB_ACCOUNT_ID)" ]; then \
		echo "ERROR: HUB_ACCOUNT_ID required"; \
		exit 1; \
	fi
	@aws cloudformation create-stack-set \
		--stack-set-name $(STACK_NAME)-spoke-minimal-stackset \
		--template-body file://cloudformation/centralized-spoke-minimal.yaml \
		--parameters \
			ParameterKey=SecurityAccountId,ParameterValue=$(HUB_ACCOUNT_ID) \
			ParameterKey=ScannerExternalId,ParameterValue=$(EXTERNAL_ID) \
		--capabilities CAPABILITY_NAMED_IAM \
		--permission-model SERVICE_MANAGED \
		--auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
		--region $(AWS_REGION) 2>/dev/null || \
		aws cloudformation update-stack-set \
			--stack-set-name $(STACK_NAME)-spoke-minimal-stackset \
			--template-body file://cloudformation/centralized-spoke-minimal.yaml \
			--parameters \
				ParameterKey=SecurityAccountId,ParameterValue=$(HUB_ACCOUNT_ID) \
				ParameterKey=ScannerExternalId,ParameterValue=$(EXTERNAL_ID) \
			--capabilities CAPABILITY_NAMED_IAM \
			--region $(AWS_REGION)
	@echo "Creating stack instances in OUs: $(ORG_UNIT_IDS)..."
	@for region in $$(echo "$(REGIONS)" | tr ',' ' '); do \
		echo "Creating instances in $$region..."; \
		aws cloudformation create-stack-instances \
			--stack-set-name $(STACK_NAME)-spoke-minimal-stackset \
			--deployment-targets OrganizationalUnitIds=$(ORG_UNIT_IDS) \
			--regions $$region \
			--operation-preferences FailureTolerancePercentage=10,MaxConcurrentPercentage=25 \
			--region $(AWS_REGION); \
	done
	@echo ""
	@echo "Minimal spoke StackSet deployment initiated!"

delete-hub:
	@aws cloudformation delete-stack \
		--stack-name $(STACK_NAME)-hub \
		--region $(AWS_REGION)
	@aws cloudformation wait stack-delete-complete \
		--stack-name $(STACK_NAME)-hub \
		--region $(AWS_REGION)
	@echo "Hub deleted"


clean:
	@rm -rf build/
	@echo "Build artifacts cleaned"

delete-bucket:
	@if [ -z "$(BUCKET_NAME)" ]; then \
		echo "ERROR: BUCKET_NAME required"; \
		echo "Usage: make delete-bucket BUCKET_NAME=my-bucket"; \
		exit 1; \
	fi
	@echo "Emptying bucket $(BUCKET_NAME)..."
	@aws s3api list-object-versions --bucket $(BUCKET_NAME) --query 'Versions[].{Key:Key,VersionId:VersionId}' --output json 2>/dev/null | \
		jq -c 'select(. != null) | .[] | select(. != null)' | \
		while read obj; do \
			key=$$(echo $$obj | jq -r '.Key'); \
			vid=$$(echo $$obj | jq -r '.VersionId'); \
			aws s3api delete-object --bucket $(BUCKET_NAME) --key "$$key" --version-id "$$vid" 2>/dev/null || true; \
		done
	@aws s3api list-object-versions --bucket $(BUCKET_NAME) --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' --output json 2>/dev/null | \
		jq -c 'select(. != null) | .[] | select(. != null)' | \
		while read obj; do \
			key=$$(echo $$obj | jq -r '.Key'); \
			vid=$$(echo $$obj | jq -r '.VersionId'); \
			aws s3api delete-object --bucket $(BUCKET_NAME) --key "$$key" --version-id "$$vid" 2>/dev/null || true; \
		done
	@aws s3 rb s3://$(BUCKET_NAME) --force 2>/dev/null || true
	@echo "Bucket $(BUCKET_NAME) deleted"

delete-buckets:
	@echo "Deleting S3 buckets for stack $(STACK_NAME)..."
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	for bucket_suffix in "artifacts" "scan-results" "cloudtrail"; do \
		BUCKET="$(STACK_NAME)-$$bucket_suffix-$$ACCOUNT_ID"; \
		if aws s3api head-bucket --bucket "$$BUCKET" 2>/dev/null; then \
			echo "Deleting bucket: $$BUCKET"; \
			$(MAKE) delete-bucket BUCKET_NAME=$$BUCKET; \
		else \
			echo "Bucket $$BUCKET does not exist, skipping"; \
		fi; \
	done
	@echo "All buckets cleaned up"

delete-dynamodb:
	@echo "Deleting DynamoDB scan cache table..."
	@aws dynamodb delete-table \
		--table-name "$(STACK_NAME)-scan-cache" \
		--region $(AWS_REGION) 2>/dev/null && \
		echo "Table $(STACK_NAME)-scan-cache deleted" || \
		echo "Table $(STACK_NAME)-scan-cache not found or already deleted"

delete-dlq:
	@echo "Deleting SQS Dead Letter Queue..."
	@QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(STACK_NAME)-scanner-dlq" \
		--region $(AWS_REGION) \
		--query 'QueueUrl' --output text 2>/dev/null); \
	if [ -n "$$QUEUE_URL" ] && [ "$$QUEUE_URL" != "None" ]; then \
		aws sqs delete-queue --queue-url "$$QUEUE_URL" --region $(AWS_REGION); \
		echo "Queue $(STACK_NAME)-scanner-dlq deleted"; \
	else \
		echo "Queue $(STACK_NAME)-scanner-dlq not found or already deleted"; \
	fi

delete-sns:
	@echo "Deleting SNS topic..."
	@TOPIC_ARN=$$(aws sns list-topics --region $(AWS_REGION) --query "Topics[?contains(TopicArn, '$(STACK_NAME)-scan-notifications')].TopicArn" --output text 2>/dev/null); \
	if [ -n "$$TOPIC_ARN" ] && [ "$$TOPIC_ARN" != "None" ]; then \
		aws sns delete-topic --topic-arn "$$TOPIC_ARN" --region $(AWS_REGION); \
		echo "Topic deleted: $$TOPIC_ARN"; \
	else \
		echo "SNS topic $(STACK_NAME)-scan-notifications not found or already deleted"; \
	fi

delete-log-groups:
	@echo "Deleting CloudWatch Log Groups..."
	@for log_group in "/aws/lambda/$(STACK_NAME)-scanner" "/aws/lambda/$(STACK_NAME)-bulk-scan" "/aws/cloudtrail/$(STACK_NAME)"; do \
		if aws logs describe-log-groups --log-group-name-prefix "$$log_group" --region $(AWS_REGION) --query 'logGroups[0].logGroupName' --output text 2>/dev/null | grep -q "$$log_group"; then \
			aws logs delete-log-group --log-group-name "$$log_group" --region $(AWS_REGION) 2>/dev/null && \
			echo "Deleted log group: $$log_group" || true; \
		else \
			echo "Log group $$log_group not found, skipping"; \
		fi; \
	done

delete-alarms:
	@echo "Deleting CloudWatch Alarms..."
	@ALARMS=$$(aws cloudwatch describe-alarms \
		--alarm-name-prefix "$(STACK_NAME)-" \
		--region $(AWS_REGION) \
		--query 'MetricAlarms[].AlarmName' \
		--output text 2>/dev/null); \
	if [ -n "$$ALARMS" ]; then \
		for alarm in $$ALARMS; do \
			echo "Deleting alarm: $$alarm"; \
			aws cloudwatch delete-alarms --alarm-names "$$alarm" --region $(AWS_REGION); \
		done; \
	else \
		echo "No alarms found with prefix $(STACK_NAME)-"; \
	fi

delete-eventbridge-rules:
	@echo "Deleting EventBridge Rules..."
	@for rule in "$(STACK_NAME)-lambda-create" "$(STACK_NAME)-lambda-update-code" "$(STACK_NAME)-lambda-update-config" "$(STACK_NAME)-bulk-scan-schedule"; do \
		if aws events describe-rule --name "$$rule" --region $(AWS_REGION) 2>/dev/null; then \
			echo "Removing targets from rule: $$rule"; \
			TARGETS=$$(aws events list-targets-by-rule --rule "$$rule" --region $(AWS_REGION) --query 'Targets[].Id' --output text 2>/dev/null); \
			if [ -n "$$TARGETS" ]; then \
				aws events remove-targets --rule "$$rule" --ids $$TARGETS --region $(AWS_REGION) 2>/dev/null || true; \
			fi; \
			echo "Deleting rule: $$rule"; \
			aws events delete-rule --name "$$rule" --region $(AWS_REGION) 2>/dev/null || true; \
		fi; \
	done

delete-kms-key:
	@echo "Scheduling KMS key for deletion..."
	@KEY_ID=$$(aws kms list-aliases --region $(AWS_REGION) \
		--query "Aliases[?AliasName=='alias/$(STACK_NAME)-scanner'].TargetKeyId" \
		--output text 2>/dev/null); \
	if [ -n "$$KEY_ID" ] && [ "$$KEY_ID" != "None" ]; then \
		echo "Deleting alias alias/$(STACK_NAME)-scanner..."; \
		aws kms delete-alias --alias-name "alias/$(STACK_NAME)-scanner" --region $(AWS_REGION) 2>/dev/null || true; \
		echo "Scheduling key $$KEY_ID for deletion (30-day wait)..."; \
		aws kms schedule-key-deletion --key-id "$$KEY_ID" --pending-window-in-days 7 --region $(AWS_REGION) 2>/dev/null && \
			echo "KMS key scheduled for deletion in 7 days" || \
			echo "Could not schedule key deletion (may already be scheduled or deleted)"; \
	else \
		echo "KMS key alias/$(STACK_NAME)-scanner not found"; \
	fi

delete-artifacts-bucket:
	@echo "Deleting artifacts bucket..."
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	BUCKET="qualys-scanner-artifacts-$$ACCOUNT_ID"; \
	if aws s3api head-bucket --bucket "$$BUCKET" 2>/dev/null; then \
		$(MAKE) delete-bucket BUCKET_NAME=$$BUCKET; \
	else \
		echo "Bucket $$BUCKET does not exist"; \
	fi

delete-secret:
	@echo "Deleting Secrets Manager secret..."
	@aws secretsmanager delete-secret \
		--secret-id "$(STACK_NAME)-qualys-credentials" \
		--force-delete-without-recovery \
		--region $(AWS_REGION) 2>/dev/null && \
		echo "Secret $(STACK_NAME)-qualys-credentials deleted" || \
		echo "Secret $(STACK_NAME)-qualys-credentials not found or already deleted"

delete-layers:
	@echo "Deleting Lambda layer versions for $(LAYER_NAME)..."
	@VERSIONS=$$(aws lambda list-layer-versions \
		--layer-name $(LAYER_NAME) \
		--region $(AWS_REGION) \
		--query 'LayerVersions[].Version' \
		--output text 2>/dev/null); \
	if [ -n "$$VERSIONS" ]; then \
		for v in $$VERSIONS; do \
			echo "Deleting $(LAYER_NAME) version $$v..."; \
			aws lambda delete-layer-version \
				--layer-name $(LAYER_NAME) \
				--version-number $$v \
				--region $(AWS_REGION); \
		done; \
		echo "All layer versions deleted"; \
	else \
		echo "No layer versions found for $(LAYER_NAME)"; \
	fi

clean-all:
	@echo "=========================================="
	@echo "COMPLETE CLEANUP - Single Account"
	@echo "Stack: $(STACK_NAME)"
	@echo "Region: $(AWS_REGION)"
	@echo "Layer: $(LAYER_NAME)"
	@echo "=========================================="
	@echo ""
	@echo "Step 1/10: Deleting CloudFormation stack..."
	-@$(MAKE) delete 2>/dev/null || echo "Stack already deleted or does not exist"
	@echo ""
	@echo "Step 2/10: Deleting S3 buckets..."
	-@$(MAKE) delete-buckets 2>/dev/null || true
	@echo ""
	@echo "Step 3/10: Deleting Secrets Manager secret (created before stack)..."
	-@$(MAKE) delete-secret 2>/dev/null || true
	@echo ""
	@echo "Step 4/10: Deleting Lambda layers (created before stack)..."
	-@$(MAKE) delete-layers 2>/dev/null || true
	@echo ""
	@echo "Step 5/10: Deleting DynamoDB table (if orphaned)..."
	-@$(MAKE) delete-dynamodb 2>/dev/null || true
	@echo ""
	@echo "Step 6/10: Deleting SQS Dead Letter Queue (if orphaned)..."
	-@$(MAKE) delete-dlq 2>/dev/null || true
	@echo ""
	@echo "Step 7/10: Deleting SNS topic (if orphaned)..."
	-@$(MAKE) delete-sns 2>/dev/null || true
	@echo ""
	@echo "Step 8/10: Deleting CloudWatch Log Groups..."
	-@$(MAKE) delete-log-groups 2>/dev/null || true
	@echo ""
	@echo "Step 9/10: Deleting CloudWatch Alarms (if orphaned)..."
	-@$(MAKE) delete-alarms 2>/dev/null || true
	@echo ""
	@echo "Step 10/10: Cleaning local build artifacts..."
	@$(MAKE) clean
	@echo ""
	@echo "=========================================="
	@echo "CLEANUP COMPLETE"
	@echo "=========================================="
	@echo ""
	@echo "Note: KMS keys created by the stack are scheduled for deletion automatically"
	@echo "      (30-day wait period enforced by AWS)."
	@echo ""
	@echo "To manually schedule KMS key deletion:"
	@echo "  make delete-kms-key"
	@echo ""
	@echo "To verify all resources are cleaned up:"
	@echo "  make clean-dry-run"

clean-all-hub:
	@echo "=========================================="
	@echo "COMPLETE CLEANUP - Hub-Spoke Deployment"
	@echo "Stack: $(STACK_NAME)-hub"
	@echo "Region: $(AWS_REGION)"
	@echo "Layer: $(LAYER_NAME)"
	@echo "=========================================="
	@echo ""
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "WARNING: ORG_UNIT_IDS not set - spoke StackSet cleanup will be skipped"; \
		echo "To clean spokes: make clean-all-hub ORG_UNIT_IDS=ou-xxxx"; \
	else \
		echo "Step 1/12: Deleting spoke StackSet..."; \
		$(MAKE) delete-spoke-stackset 2>/dev/null || echo "Spoke StackSet not found"; \
	fi
	@echo ""
	@echo "Step 2/12: Deleting hub stack..."
	-@$(MAKE) delete-hub 2>/dev/null || echo "Hub stack already deleted"
	@echo ""
	@echo "Step 3/12: Deleting artifacts bucket..."
	-@$(MAKE) delete-artifacts-bucket 2>/dev/null || true
	@echo ""
	@echo "Step 4/12: Deleting Secrets Manager secret (hub)..."
	-@aws secretsmanager delete-secret \
		--secret-id "$(STACK_NAME)-hub-qualys-credentials" \
		--force-delete-without-recovery \
		--region $(AWS_REGION) 2>/dev/null || echo "Secret $(STACK_NAME)-hub-qualys-credentials not found"
	@echo ""
	@echo "Step 5/12: Deleting Lambda layers ($(LAYER_NAME))..."
	-@$(MAKE) delete-layers 2>/dev/null || true
	@echo ""
	@echo "Step 6/12: Deleting DynamoDB table (if orphaned)..."
	-@aws dynamodb delete-table \
		--table-name "$(STACK_NAME)-hub-scan-cache" \
		--region $(AWS_REGION) 2>/dev/null || true
	@echo ""
	@echo "Step 7/12: Deleting SQS Dead Letter Queue (if orphaned)..."
	-@QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(STACK_NAME)-hub-scanner-dlq" \
		--region $(AWS_REGION) \
		--query 'QueueUrl' --output text 2>/dev/null); \
	if [ -n "$$QUEUE_URL" ] && [ "$$QUEUE_URL" != "None" ]; then \
		aws sqs delete-queue --queue-url "$$QUEUE_URL" --region $(AWS_REGION); \
	fi
	@echo ""
	@echo "Step 8/12: Deleting SNS topic (if orphaned)..."
	-@TOPIC_ARN=$$(aws sns list-topics --region $(AWS_REGION) --query "Topics[?contains(TopicArn, '$(STACK_NAME)-hub-scan-notifications')].TopicArn" --output text 2>/dev/null); \
	if [ -n "$$TOPIC_ARN" ] && [ "$$TOPIC_ARN" != "None" ]; then \
		aws sns delete-topic --topic-arn "$$TOPIC_ARN" --region $(AWS_REGION); \
	fi
	@echo ""
	@echo "Step 9/12: Deleting CloudWatch Log Groups..."
	-@for log_group in "/aws/lambda/$(STACK_NAME)-hub-scanner" "/aws/lambda/$(STACK_NAME)-hub-bulk-scan"; do \
		aws logs delete-log-group --log-group-name "$$log_group" --region $(AWS_REGION) 2>/dev/null || true; \
	done
	@echo ""
	@echo "Step 10/12: Deleting CloudWatch Alarms (if orphaned)..."
	-@ALARMS=$$(aws cloudwatch describe-alarms \
		--alarm-name-prefix "$(STACK_NAME)-hub-" \
		--region $(AWS_REGION) \
		--query 'MetricAlarms[].AlarmName' \
		--output text 2>/dev/null); \
	if [ -n "$$ALARMS" ]; then \
		for alarm in $$ALARMS; do \
			aws cloudwatch delete-alarms --alarm-names "$$alarm" --region $(AWS_REGION); \
		done; \
	fi
	@echo ""
	@echo "Step 11/12: Deleting Central EventBridge Bus (if orphaned)..."
	-@aws events delete-event-bus \
		--name "$(STACK_NAME)-hub-central-bus" \
		--region $(AWS_REGION) 2>/dev/null || true
	@echo ""
	@echo "Step 12/12: Cleaning local build artifacts..."
	@$(MAKE) clean
	@echo ""
	@echo "=========================================="
	@echo "HUB-SPOKE CLEANUP COMPLETE"
	@echo "=========================================="
	@echo ""
	@echo "Note: KMS keys are scheduled for deletion automatically (30-day wait)"
	@echo ""
	@echo "To verify all resources are cleaned up:"
	@echo "  make clean-dry-run"

clean-all-stackset:
	@echo "=========================================="
	@echo "COMPLETE CLEANUP - StackSet Deployment"
	@echo "StackSet: $(STACK_NAME)-stackset"
	@echo "Region: $(AWS_REGION)"
	@echo "Layer: $(LAYER_NAME)"
	@echo "=========================================="
	@echo ""
	@if [ -z "$(ORG_UNIT_IDS)" ]; then \
		echo "ERROR: ORG_UNIT_IDS required for StackSet cleanup"; \
		echo "Usage: make clean-all-stackset ORG_UNIT_IDS=ou-xxxx"; \
		exit 1; \
	fi
	@echo "Step 1/5: Deleting StackSet (this deletes all member account stacks)..."
	-@$(MAKE) delete-stackset 2>/dev/null || echo "StackSet not found"
	@echo ""
	@echo "Step 2/5: Deleting artifacts bucket..."
	-@$(MAKE) delete-artifacts-bucket 2>/dev/null || true
	@echo ""
	@echo "Step 3/5: Deleting Lambda layers ($(LAYER_NAME))..."
	-@$(MAKE) delete-layers 2>/dev/null || true
	@echo ""
	@echo "Step 4/5: Deleting scan-results bucket (admin account, if created)..."
	-@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	BUCKET="qualys-lambda-scan-results-$$ACCOUNT_ID"; \
	if aws s3api head-bucket --bucket "$$BUCKET" 2>/dev/null; then \
		$(MAKE) delete-bucket BUCKET_NAME=$$BUCKET; \
	fi
	@echo ""
	@echo "Step 5/5: Cleaning local build artifacts..."
	@$(MAKE) clean
	@echo ""
	@echo "=========================================="
	@echo "STACKSET CLEANUP COMPLETE"
	@echo "=========================================="
	@echo ""
	@echo "Note: Member account resources are deleted via StackSet deletion."
	@echo "      KMS keys in member accounts have 30-day deletion wait period."
	@echo ""
	@echo "If any member account resources remain orphaned, use these commands in each account:"
	@echo "  - Secret: aws secretsmanager delete-secret --secret-id qualys-lambda-scanner-credentials --force-delete-without-recovery"
	@echo "  - Log Groups: aws logs delete-log-group --log-group-name /aws/lambda/qualys-lambda-scanner"

clean-dry-run:
	@echo "=========================================="
	@echo "DRY RUN - Resources that would be deleted"
	@echo "=========================================="
	@echo ""
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	echo "Account ID: $$ACCOUNT_ID"; \
	echo "Region: $(AWS_REGION)"; \
	echo "Stack Name: $(STACK_NAME)"; \
	echo "Layer Name: $(LAYER_NAME)"; \
	echo ""
	@echo "=== CloudFormation Stacks ==="
	@aws cloudformation describe-stacks --stack-name $(STACK_NAME) --region $(AWS_REGION) \
		--query 'Stacks[0].StackName' --output text 2>/dev/null && \
		echo "  [FOUND] $(STACK_NAME)" || echo "  [NOT FOUND] $(STACK_NAME)"
	@aws cloudformation describe-stacks --stack-name $(STACK_NAME)-hub --region $(AWS_REGION) \
		--query 'Stacks[0].StackName' --output text 2>/dev/null && \
		echo "  [FOUND] $(STACK_NAME)-hub" || echo "  [NOT FOUND] $(STACK_NAME)-hub"
	@echo ""
	@echo "=== S3 Buckets ==="
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	for bucket in "$(STACK_NAME)-artifacts-$$ACCOUNT_ID" "$(STACK_NAME)-scan-results-$$ACCOUNT_ID" "$(STACK_NAME)-cloudtrail-$$ACCOUNT_ID" "qualys-scanner-artifacts-$$ACCOUNT_ID" "$(STACK_NAME)-hub-scan-results-$$ACCOUNT_ID" "qualys-lambda-scan-results-$$ACCOUNT_ID"; do \
		if aws s3api head-bucket --bucket "$$bucket" 2>/dev/null; then \
			OBJECTS=$$(aws s3 ls s3://$$bucket --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $$3}' || echo "?"); \
			echo "  [FOUND] $$bucket ($$OBJECTS objects)"; \
		fi; \
	done
	@echo ""
	@echo "=== Secrets Manager Secrets ==="
	@for secret in "$(STACK_NAME)-qualys-credentials" "$(STACK_NAME)-hub-qualys-credentials" "qualys-lambda-scanner-credentials"; do \
		if aws secretsmanager describe-secret --secret-id "$$secret" --region $(AWS_REGION) 2>/dev/null >/dev/null; then \
			echo "  [FOUND] $$secret"; \
		fi; \
	done
	@echo ""
	@echo "=== Lambda Layers ==="
	@VERSIONS=$$(aws lambda list-layer-versions --layer-name $(LAYER_NAME) --region $(AWS_REGION) \
		--query 'LayerVersions[].Version' --output text 2>/dev/null); \
	if [ -n "$$VERSIONS" ]; then \
		echo "  [FOUND] $(LAYER_NAME): versions $$VERSIONS"; \
	else \
		echo "  [NOT FOUND] $(LAYER_NAME)"; \
	fi
	@echo ""
	@echo "=== DynamoDB Tables ==="
	@for table in "$(STACK_NAME)-scan-cache" "$(STACK_NAME)-hub-scan-cache" "qualys-lambda-scanner-cache"; do \
		if aws dynamodb describe-table --table-name "$$table" --region $(AWS_REGION) 2>/dev/null >/dev/null; then \
			echo "  [FOUND] $$table"; \
		fi; \
	done
	@echo ""
	@echo "=== SQS Queues ==="
	@for queue in "$(STACK_NAME)-scanner-dlq" "$(STACK_NAME)-hub-scanner-dlq" "qualys-lambda-scanner-dlq"; do \
		if aws sqs get-queue-url --queue-name "$$queue" --region $(AWS_REGION) 2>/dev/null >/dev/null; then \
			echo "  [FOUND] $$queue"; \
		fi; \
	done
	@echo ""
	@echo "=== SNS Topics ==="
	@TOPICS=$$(aws sns list-topics --region $(AWS_REGION) --query "Topics[?contains(TopicArn, 'scan-notifications')].TopicArn" --output text 2>/dev/null); \
	if [ -n "$$TOPICS" ]; then \
		for topic in $$TOPICS; do \
			echo "  [FOUND] $$topic"; \
		done; \
	fi
	@echo ""
	@echo "=== CloudWatch Log Groups ==="
	@for prefix in "/aws/lambda/$(STACK_NAME)" "/aws/cloudtrail/$(STACK_NAME)" "/aws/lambda/qualys-lambda-scanner" "/aws/lambda/qualys-lambda-bulk-scan"; do \
		GROUPS=$$(aws logs describe-log-groups --log-group-name-prefix "$$prefix" --region $(AWS_REGION) \
			--query 'logGroups[].logGroupName' --output text 2>/dev/null); \
		if [ -n "$$GROUPS" ]; then \
			for group in $$GROUPS; do \
				echo "  [FOUND] $$group"; \
			done; \
		fi; \
	done
	@echo ""
	@echo "=== CloudWatch Alarms ==="
	@ALARMS=$$(aws cloudwatch describe-alarms --alarm-name-prefix "$(STACK_NAME)-" --region $(AWS_REGION) \
		--query 'MetricAlarms[].AlarmName' --output text 2>/dev/null); \
	if [ -n "$$ALARMS" ]; then \
		for alarm in $$ALARMS; do \
			echo "  [FOUND] $$alarm"; \
		done; \
	else \
		echo "  [NOT FOUND] No alarms with prefix $(STACK_NAME)-"; \
	fi
	@ALARMS2=$$(aws cloudwatch describe-alarms --alarm-name-prefix "qualys-lambda-scanner-" --region $(AWS_REGION) \
		--query 'MetricAlarms[].AlarmName' --output text 2>/dev/null); \
	if [ -n "$$ALARMS2" ]; then \
		for alarm in $$ALARMS2; do \
			echo "  [FOUND] $$alarm"; \
		done; \
	fi
	@echo ""
	@echo "=== EventBridge Rules ==="
	@for rule in "$(STACK_NAME)-lambda-create" "$(STACK_NAME)-lambda-update-code" "$(STACK_NAME)-lambda-update-config" "$(STACK_NAME)-bulk-scan-schedule" "qualys-lambda-scanner-create" "qualys-lambda-scanner-update-code" "qualys-lambda-scanner-update-config"; do \
		if aws events describe-rule --name "$$rule" --region $(AWS_REGION) 2>/dev/null >/dev/null; then \
			echo "  [FOUND] $$rule"; \
		fi; \
	done
	@echo ""
	@echo "=== EventBridge Event Buses ==="
	@for bus in "$(STACK_NAME)-central-bus" "$(STACK_NAME)-hub-central-bus"; do \
		if aws events describe-event-bus --name "$$bus" --region $(AWS_REGION) 2>/dev/null >/dev/null; then \
			echo "  [FOUND] $$bus"; \
		fi; \
	done
	@echo ""
	@echo "=== KMS Keys ==="
	@KEY_ID=$$(aws kms list-aliases --region $(AWS_REGION) \
		--query "Aliases[?AliasName=='alias/$(STACK_NAME)-scanner'].TargetKeyId" \
		--output text 2>/dev/null); \
	if [ -n "$$KEY_ID" ] && [ "$$KEY_ID" != "None" ]; then \
		STATE=$$(aws kms describe-key --key-id "$$KEY_ID" --region $(AWS_REGION) --query 'KeyMetadata.KeyState' --output text 2>/dev/null); \
		echo "  [FOUND] alias/$(STACK_NAME)-scanner (Key: $$KEY_ID, State: $$STATE)"; \
	fi
	@KEY_ID2=$$(aws kms list-aliases --region $(AWS_REGION) \
		--query "Aliases[?AliasName=='alias/qualys-lambda-scanner'].TargetKeyId" \
		--output text 2>/dev/null); \
	if [ -n "$$KEY_ID2" ] && [ "$$KEY_ID2" != "None" ]; then \
		STATE=$$(aws kms describe-key --key-id "$$KEY_ID2" --region $(AWS_REGION) --query 'KeyMetadata.KeyState' --output text 2>/dev/null); \
		echo "  [FOUND] alias/qualys-lambda-scanner (Key: $$KEY_ID2, State: $$STATE)"; \
	fi
	@echo ""
	@echo "=== StackSets ==="
	@aws cloudformation describe-stack-set --stack-set-name $(STACK_NAME)-stackset \
		--region $(AWS_REGION) --query 'StackSet.StackSetName' --output text 2>/dev/null && \
		echo "  [FOUND] $(STACK_NAME)-stackset" || true
	@aws cloudformation describe-stack-set --stack-set-name $(STACK_NAME)-spoke-stackset \
		--region $(AWS_REGION) --query 'StackSet.StackSetName' --output text 2>/dev/null && \
		echo "  [FOUND] $(STACK_NAME)-spoke-stackset" || true
	@echo ""
	@echo "=== Local Build Artifacts ==="
	@if [ -d build ]; then \
		echo "  [FOUND] build/ directory:"; \
		ls -la build/ 2>/dev/null | head -10 || true; \
	else \
		echo "  [NOT FOUND] build/ directory"; \
	fi
	@echo ""
	@echo "=========================================="
	@echo ""
	@echo "To perform cleanup, run one of:"
	@echo "  make clean-all                              # Single account deployment"
	@echo "  make clean-all-hub ORG_UNIT_IDS=ou-xxx      # Hub-spoke deployment"
	@echo "  make clean-all-stackset ORG_UNIT_IDS=ou-xxx # StackSet deployment"

# =============================================================================
# Testing Targets
# =============================================================================

install-dev:
	@echo "Installing development dependencies..."
	pip3 install -e ".[dev]"
	@echo "Development dependencies installed"

test: test-unit
	@echo "All tests completed"

test-unit:
	@echo "Running unit tests..."
	python3 -m pytest tests/unit -v -m unit --tb=short

test-integration:
	@echo "Running integration tests (requires AWS credentials)..."
	python3 -m pytest tests/integration -v -m integration --tb=short

test-smoke:
	@echo "Running smoke test..."
	python3 -m pytest tests/integration/test_smoke.py -v -m smoke --tb=short

test-bulk-dry-run:
	@echo "Testing bulk scan in dry-run mode..."
	@if [ -z "$(SCANNER_FUNCTION_NAME)" ]; then \
		SCANNER_FUNCTION_NAME=$$(aws cloudformation describe-stacks \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='BulkScanLambdaName'].OutputValue" \
			--output text 2>/dev/null); \
	fi; \
	if [ -z "$$SCANNER_FUNCTION_NAME" ] || [ "$$SCANNER_FUNCTION_NAME" = "None" ]; then \
		echo "ERROR: Could not find bulk scan function. Deploy first or set SCANNER_FUNCTION_NAME"; \
		exit 1; \
	fi; \
	echo "Invoking bulk scan with dry_run=true..."; \
	aws lambda invoke \
		--function-name $$SCANNER_FUNCTION_NAME \
		--payload '{"dry_run": true, "regions": ["$(AWS_REGION)"]}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/bulk-scan-dry-run-output.json; \
	echo ""; \
	echo "Results:"; \
	cat /tmp/bulk-scan-dry-run-output.json | python3 -m json.tool

test-coverage:
	@echo "Running tests with coverage..."
	python3 -m pytest tests/unit -v --cov=scanner-lambda --cov-report=term-missing --cov-report=html
	@echo "Coverage report generated in htmlcov/"

# =============================================================================
# Validation Targets
# =============================================================================

validate:
	@echo "Running pre-flight validation..."
	@python3 scripts/validate.py --type $(if $(ORG_UNIT_IDS),stackset,single-account)

validate-cfn:
	@echo "Linting CloudFormation templates..."
	@if command -v cfn-lint >/dev/null 2>&1; then \
		cfn-lint cloudformation/*.yaml; \
		echo "All templates passed validation"; \
	else \
		echo "cfn-lint not installed. Install with: pip install cfn-lint"; \
		exit 1; \
	fi

validate-config:
	@echo "Validating configuration..."
	@python3 scripts/config_loader.py --config .qualys-scanner.yml 2>/dev/null || \
		echo "No .qualys-scanner.yml found (using defaults)"

# =============================================================================
# Configuration Targets
# =============================================================================

config-init:
	@if [ -f .qualys-scanner.yml ]; then \
		echo "ERROR: .qualys-scanner.yml already exists"; \
		echo "Remove it first or edit directly"; \
		exit 1; \
	fi
	@cp .qualys-scanner.yml.example .qualys-scanner.yml
	@echo "Created .qualys-scanner.yml from example"
	@echo "Edit this file to customize your deployment settings"

config-show:
	@echo "Current configuration:"
	@echo ""
	@if [ -f .qualys-scanner.yml ]; then \
		python3 scripts/config_loader.py; \
	else \
		echo "No .qualys-scanner.yml found. Using defaults:"; \
		echo ""; \
		python3 scripts/config_loader.py; \
	fi

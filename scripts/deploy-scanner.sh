#!/bin/bash
# Script to deploy the Qualys Lambda Scanner

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
DEPLOYMENT_TYPE="single-account"
STACK_NAME="qualys-lambda-scanner"
QUALYS_POD="US2"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Function to display usage
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -t, --type TYPE           Deployment type: single-account, stackset, or centralized (default: single-account)"
    echo "  -n, --name NAME           Stack name (default: qualys-lambda-scanner)"
    echo "  -p, --pod POD             Qualys POD (default: US2)"
    echo "  -r, --region REGION       AWS region (default: us-east-1)"
    echo "  -i, --image-uri URI       Scanner Lambda container image URI (required for Docker deployment)"
    echo "  -l, --layer-arn ARN       QScanner Lambda layer ARN (required for Layer deployment)"
    echo "  -s, --secret-arn ARN      Qualys credentials secret ARN (optional, will create if not provided)"
    echo "  -h, --help                Display this help message"
    echo ""
    echo "Example:"
    echo "  $0 --type single-account --pod US2 --layer-arn arn:aws:lambda:us-east-1:123456789012:layer:qscanner:1"
    exit 1
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -t|--type)
            DEPLOYMENT_TYPE="$2"
            shift 2
            ;;
        -n|--name)
            STACK_NAME="$2"
            shift 2
            ;;
        -p|--pod)
            QUALYS_POD="$2"
            shift 2
            ;;
        -r|--region)
            AWS_REGION="$2"
            shift 2
            ;;
        -i|--image-uri)
            IMAGE_URI="$2"
            shift 2
            ;;
        -l|--layer-arn)
            LAYER_ARN="$2"
            shift 2
            ;;
        -s|--secret-arn)
            SECRET_ARN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# Validate deployment type
if [[ ! "$DEPLOYMENT_TYPE" =~ ^(single-account|stackset|centralized)$ ]]; then
    echo -e "${RED}Error: Invalid deployment type. Must be single-account, stackset, or centralized${NC}"
    exit 1
fi

echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║        Qualys Lambda Scanner Deployment Script            ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}Deployment Configuration:${NC}"
echo -e "  Type:        ${YELLOW}${DEPLOYMENT_TYPE}${NC}"
echo -e "  Stack Name:  ${YELLOW}${STACK_NAME}${NC}"
echo -e "  Qualys POD:  ${YELLOW}${QUALYS_POD}${NC}"
echo -e "  AWS Region:  ${YELLOW}${AWS_REGION}${NC}"
echo ""

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed${NC}"
    exit 1
fi

# Check AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS credentials not configured${NC}"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo -e "${GREEN}AWS Account ID: ${YELLOW}${ACCOUNT_ID}${NC}"
echo ""

# Prompt for Qualys access token if secret not provided
if [ -z "$SECRET_ARN" ]; then
    echo -e "${YELLOW}Qualys credentials secret not provided. You'll need to enter the access token.${NC}"
    read -sp "Enter Qualys Access Token: " QUALYS_TOKEN
    echo ""

    # Create secret
    echo -e "${YELLOW}Creating Secrets Manager secret...${NC}"
    SECRET_ARN=$(aws secretsmanager create-secret \
        --name "${STACK_NAME}-qualys-credentials" \
        --description "Qualys credentials for Lambda scanner" \
        --secret-string "{\"qualys_pod\":\"${QUALYS_POD}\",\"qualys_access_token\":\"${QUALYS_TOKEN}\"}" \
        --region ${AWS_REGION} \
        --query ARN \
        --output text)

    echo -e "${GREEN}Secret created: ${SECRET_ARN}${NC}"
fi

# Deploy based on type
case $DEPLOYMENT_TYPE in
    single-account)
        echo -e "${YELLOW}Deploying single-account stack...${NC}"

        if [ -n "$IMAGE_URI" ]; then
            # Docker-based deployment
            aws cloudformation deploy \
                --template-file cloudformation/single-account.yaml \
                --stack-name ${STACK_NAME} \
                --parameter-overrides \
                    QualysPod=${QUALYS_POD} \
                    ScannerImageUri=${IMAGE_URI} \
                --capabilities CAPABILITY_NAMED_IAM \
                --region ${AWS_REGION}
        elif [ -n "$LAYER_ARN" ]; then
            # Layer-based deployment (using simplified template)
            echo -e "${YELLOW}Layer-based deployment not yet implemented. Use Docker-based deployment.${NC}"
            exit 1
        else
            echo -e "${RED}Error: Either --image-uri or --layer-arn must be provided${NC}"
            exit 1
        fi
        ;;

    stackset)
        echo -e "${YELLOW}Deploying StackSet...${NC}"
        echo -e "${YELLOW}Note: You'll need to specify target accounts and regions separately${NC}"

        aws cloudformation create-stack-set \
            --stack-set-name ${STACK_NAME} \
            --template-body file://cloudformation/stackset.yaml \
            --parameters \
                ParameterKey=QualysPod,ParameterValue=${QUALYS_POD} \
                ParameterKey=ScannerImageUri,ParameterValue=${IMAGE_URI} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region ${AWS_REGION}

        echo -e "${GREEN}StackSet created. Deploy instances with:${NC}"
        echo "  aws cloudformation create-stack-instances \\"
        echo "    --stack-set-name ${STACK_NAME} \\"
        echo "    --accounts ACCOUNT_ID1 ACCOUNT_ID2 \\"
        echo "    --regions REGION1 REGION2"
        ;;

    centralized)
        echo -e "${YELLOW}Centralized deployment not yet implemented${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Deployment Complete!                          ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "1. Test the scanner by deploying a Lambda function"
echo "2. Check CloudWatch Logs: /aws/lambda/${STACK_NAME}-scanner"
echo "3. View scan results in S3 bucket (if enabled)"
echo "4. Subscribe to SNS notifications (if enabled)"

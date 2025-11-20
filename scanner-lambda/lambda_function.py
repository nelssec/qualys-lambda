"""
AWS Lambda function to scan Lambda functions using Qualys QScanner.
Triggered by EventBridge when Lambda functions are created or updated.
"""

import os
import json
import boto3
import subprocess
import logging
from datetime import datetime
from typing import Dict, Any, Optional

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
lambda_client = boto3.client('lambda')
secrets_manager = boto3.client('secretsmanager')
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')
sts_client = boto3.client('sts')

# Environment variables
QUALYS_SECRET_ARN = os.environ.get('QUALYS_SECRET_ARN')
RESULTS_S3_BUCKET = os.environ.get('RESULTS_S3_BUCKET')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
SCAN_TIMEOUT = int(os.environ.get('SCAN_TIMEOUT', '300'))
QSCANNER_PATH = '/opt/qscanner'


class ScanException(Exception):
    """Custom exception for scan failures."""
    pass


def get_qualys_credentials() -> Dict[str, str]:
    """
    Retrieve Qualys credentials from AWS Secrets Manager.

    Returns:
        Dictionary containing Qualys credentials
    """
    try:
        response = secrets_manager.get_secret_value(SecretId=QUALYS_SECRET_ARN)
        secret = json.loads(response['SecretString'])

        required_fields = ['qualys_pod', 'qualys_access_token']
        for field in required_fields:
            if field not in secret:
                raise ValueError(f"Missing required field: {field}")

        logger.info(f"Retrieved Qualys credentials for pod: {secret['qualys_pod']}")
        return secret

    except Exception as e:
        logger.error(f"Failed to retrieve Qualys credentials: {e}")
        raise


def get_lambda_details(function_arn: str, cross_account_role: Optional[str] = None) -> Dict[str, Any]:
    """
    Get Lambda function details including image URI.

    Args:
        function_arn: ARN of the Lambda function
        cross_account_role: Optional cross-account role ARN for centralized scanning

    Returns:
        Dictionary containing Lambda function details
    """
    try:
        # Handle cross-account access if role provided
        if cross_account_role:
            logger.info(f"Assuming cross-account role: {cross_account_role}")
            assumed_role = sts_client.assume_role(
                RoleArn=cross_account_role,
                RoleSessionName='QScannerSession'
            )

            # Create Lambda client with assumed role credentials
            lambda_client_temp = boto3.client(
                'lambda',
                aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                aws_session_token=assumed_role['Credentials']['SessionToken']
            )
        else:
            lambda_client_temp = lambda_client

        # Get function configuration
        response = lambda_client_temp.get_function(FunctionName=function_arn)

        function_config = response['Configuration']
        logger.info(f"Retrieved details for Lambda: {function_config['FunctionName']}")

        return {
            'function_name': function_config['FunctionName'],
            'function_arn': function_config['FunctionArn'],
            'runtime': function_config.get('Runtime', 'N/A'),
            'package_type': function_config.get('PackageType', 'Zip'),
            'code_sha256': function_config.get('CodeSha256'),
            'image_uri': function_config.get('ImageUri'),
            'last_modified': function_config.get('LastModified'),
            'code_size': function_config.get('CodeSize'),
            'memory_size': function_config.get('MemorySize'),
            'timeout': function_config.get('Timeout'),
        }

    except Exception as e:
        logger.error(f"Failed to get Lambda details: {e}")
        raise


def run_qscanner(function_arn: str, qualys_creds: Dict[str, str], aws_region: str) -> Dict[str, Any]:
    """
    Execute QScanner against the Lambda function using the 'lambda' command.

    Args:
        function_arn: ARN or name of the Lambda function
        qualys_creds: Qualys credentials dictionary
        aws_region: AWS region where the Lambda function is located

    Returns:
        Dictionary containing scan results
    """
    try:
        logger.info(f"Starting QScanner for Lambda function: {function_arn}")

        # Build QScanner command using the 'lambda' command
        cmd = [
            QSCANNER_PATH,
            '--pod', qualys_creds['qualys_pod'],
            '--access-token', qualys_creds['qualys_access_token'],
            '--output-format', 'json',
            'lambda', function_arn
        ]

        # Set AWS environment variables
        # Lambda execution role credentials are automatically available via AWS_* env vars
        env = os.environ.copy()
        env['AWS_REGION'] = aws_region

        # Add optional registry credentials if provided (for private container registries)
        if 'registry_username' in qualys_creds:
            env['QSCANNER_REGISTRY_USERNAME'] = qualys_creds['registry_username']
        if 'registry_password' in qualys_creds:
            env['QSCANNER_REGISTRY_PASSWORD'] = qualys_creds['registry_password']
        if 'registry_token' in qualys_creds:
            env['QSCANNER_REGISTRY_TOKEN'] = qualys_creds['registry_token']

        # Execute QScanner
        logger.info(f"Executing: {' '.join(cmd[:6])} [credentials hidden] lambda {function_arn}")

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT
        )

        # Check if scan succeeded
        if result.returncode != 0:
            logger.error(f"QScanner failed with exit code {result.returncode}")
            logger.error(f"STDOUT: {result.stdout}")
            logger.error(f"STDERR: {result.stderr}")
            raise ScanException(f"QScanner failed: {result.stderr}")

        logger.info("QScanner completed successfully")
        logger.info(f"STDOUT: {result.stdout}")

        # Parse JSON output
        try:
            scan_results = json.loads(result.stdout) if result.stdout else {}
        except json.JSONDecodeError:
            logger.warning("Failed to parse QScanner output as JSON, storing raw output")
            scan_results = {
                'raw_output': result.stdout,
                'stderr': result.stderr
            }

        return {
            'success': True,
            'exit_code': result.returncode,
            'results': scan_results,
            'stdout': result.stdout,
            'stderr': result.stderr
        }

    except subprocess.TimeoutExpired:
        logger.error(f"QScanner timed out after {SCAN_TIMEOUT} seconds")
        raise ScanException(f"Scan timeout after {SCAN_TIMEOUT} seconds")

    except Exception as e:
        logger.error(f"QScanner execution failed: {e}")
        raise


def store_results(lambda_details: Dict[str, Any], scan_results: Dict[str, Any]) -> None:
    """
    Store scan results in S3 and/or send notification via SNS.

    Args:
        lambda_details: Lambda function details
        scan_results: QScanner results
    """
    timestamp = datetime.utcnow().isoformat()

    # Combine all data
    full_results = {
        'scan_timestamp': timestamp,
        'lambda_function': lambda_details,
        'scan_results': scan_results
    }

    # Store in S3 if configured
    if RESULTS_S3_BUCKET:
        try:
            key = f"scans/{lambda_details['function_name']}/{timestamp}.json"
            s3_client.put_object(
                Bucket=RESULTS_S3_BUCKET,
                Key=key,
                Body=json.dumps(full_results, indent=2),
                ContentType='application/json',
                ServerSideEncryption='AES256'
            )
            logger.info(f"Stored results in S3: s3://{RESULTS_S3_BUCKET}/{key}")
        except Exception as e:
            logger.error(f"Failed to store results in S3: {e}")

    # Send SNS notification if configured
    if SNS_TOPIC_ARN:
        try:
            # Create a summary for the SNS message
            message = {
                'function_name': lambda_details['function_name'],
                'function_arn': lambda_details['function_arn'],
                'scan_timestamp': timestamp,
                'scan_success': scan_results['success'],
                'image_uri': lambda_details.get('image_uri', 'N/A')
            }

            # Add vulnerability summary if available
            if 'results' in scan_results and isinstance(scan_results['results'], dict):
                vuln_summary = scan_results['results'].get('vulnerabilities', {})
                message['vulnerability_summary'] = vuln_summary

            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=f"QScanner Results: {lambda_details['function_name']}",
                Message=json.dumps(message, indent=2)
            )
            logger.info(f"Sent notification to SNS: {SNS_TOPIC_ARN}")
        except Exception as e:
            logger.error(f"Failed to send SNS notification: {e}")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler function.

    Args:
        event: EventBridge event containing Lambda deployment details
        context: Lambda context object

    Returns:
        Response dictionary
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        # Extract Lambda function ARN from EventBridge event
        # EventBridge event structure for Lambda API calls
        if 'detail' not in event:
            raise ValueError("Invalid event structure: missing 'detail' field")

        detail = event['detail']

        # The ARN is in different places depending on the API call
        if 'responseElements' in detail and detail['responseElements']:
            function_arn = detail['responseElements'].get('functionArn')
        elif 'requestParameters' in detail:
            # For updates, the function name might be in requestParameters
            function_name = detail['requestParameters'].get('functionName')
            if function_name:
                # Construct ARN from event metadata
                account_id = event.get('account', detail.get('userIdentity', {}).get('accountId'))
                region = event.get('region', 'us-east-1')
                function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
            else:
                raise ValueError("Could not extract function name from event")
        else:
            raise ValueError("Could not extract function ARN from event")

        if not function_arn:
            raise ValueError("Function ARN is empty")

        logger.info(f"Processing Lambda function: {function_arn}")

        # Get Qualys credentials
        qualys_creds = get_qualys_credentials()

        # Get cross-account role if specified (for centralized scanning)
        cross_account_role = os.environ.get('CROSS_ACCOUNT_ROLE_ARN')

        # Get Lambda function details
        lambda_details = get_lambda_details(function_arn, cross_account_role)

        # Extract region from event or use default
        aws_region = event.get('region', os.environ.get('AWS_REGION', 'us-east-1'))

        logger.info(f"Scanning Lambda function: {function_arn}")
        logger.info(f"Package type: {lambda_details['package_type']}")

        # Run QScanner using the 'lambda' command
        # This supports both Zip and Image package types
        scan_results = run_qscanner(function_arn, qualys_creds, aws_region)

        # Store results
        store_results(lambda_details, scan_results)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Scan completed successfully',
                'function_arn': function_arn,
                'package_type': lambda_details['package_type'],
                'scan_success': scan_results['success']
            })
        }

    except ScanException as e:
        logger.error(f"Scan failed: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Scan failed',
                'error': str(e)
            })
        }

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Internal error',
                'error': str(e)
            })
        }

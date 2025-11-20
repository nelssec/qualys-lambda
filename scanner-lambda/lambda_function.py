import os
import json
import boto3
import subprocess
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')
secrets_manager = boto3.client('secretsmanager')
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')
sts_client = boto3.client('sts')
dynamodb = boto3.resource('dynamodb')

QUALYS_SECRET_ARN = os.environ.get('QUALYS_SECRET_ARN')
RESULTS_S3_BUCKET = os.environ.get('RESULTS_S3_BUCKET')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
SCAN_CACHE_TABLE = os.environ.get('SCAN_CACHE_TABLE')
SCAN_TIMEOUT = int(os.environ.get('SCAN_TIMEOUT', '300'))
CACHE_TTL_DAYS = int(os.environ.get('CACHE_TTL_DAYS', '30'))
QSCANNER_PATH = os.environ.get('QSCANNER_PATH', '/opt/bin/qscanner')


class ScanException(Exception):
    pass


def get_qualys_credentials() -> Dict[str, str]:
    response = secrets_manager.get_secret_value(SecretId=QUALYS_SECRET_ARN)
    secret = json.loads(response['SecretString'])

    required_fields = ['qualys_pod', 'qualys_access_token']
    for field in required_fields:
        if field not in secret:
            raise ValueError(f"Missing required field: {field}")

    logger.info(f"Retrieved Qualys credentials for pod: {secret['qualys_pod']}")
    return secret


def check_scan_cache(function_arn: str, code_sha256: str) -> bool:
    if not SCAN_CACHE_TABLE or not code_sha256:
        return False

    try:
        table = dynamodb.Table(SCAN_CACHE_TABLE)
        response = table.get_item(Key={'function_arn': function_arn})

        if 'Item' not in response:
            return False

        item = response['Item']
        cached_sha256 = item.get('code_sha256')
        scan_timestamp = item.get('scan_timestamp')

        if cached_sha256 != code_sha256:
            logger.info(f"Code hash changed: {cached_sha256} -> {code_sha256}")
            return False

        if scan_timestamp:
            scan_time = datetime.fromisoformat(scan_timestamp)
            cache_expiry = scan_time + timedelta(days=CACHE_TTL_DAYS)

            if datetime.utcnow() > cache_expiry:
                logger.info(f"Cache expired (scanned {scan_timestamp})")
                return False

        logger.info(f"Cache hit: {function_arn} with hash {code_sha256}")
        return True

    except Exception as e:
        logger.error(f"Error checking scan cache: {e}")
        return False


def update_scan_cache(function_arn: str, lambda_details: Dict[str, Any], scan_results: Dict[str, Any]) -> None:
    if not SCAN_CACHE_TABLE:
        return

    try:
        table = dynamodb.Table(SCAN_CACHE_TABLE)
        timestamp = datetime.utcnow()

        table.put_item(
            Item={
                'function_arn': function_arn,
                'code_sha256': lambda_details.get('code_sha256'),
                'scan_timestamp': timestamp.isoformat(),
                'function_name': lambda_details.get('function_name'),
                'package_type': lambda_details.get('package_type'),
                'runtime': lambda_details.get('runtime'),
                'last_modified': lambda_details.get('last_modified'),
                'scan_success': scan_results.get('success'),
                'ttl': int((timestamp + timedelta(days=CACHE_TTL_DAYS)).timestamp())
            }
        )

        logger.info(f"Updated scan cache for {function_arn}")

    except Exception as e:
        logger.error(f"Failed to update scan cache: {e}")


def get_lambda_details(function_arn: str, cross_account_role: Optional[str] = None) -> Dict[str, Any]:
    if cross_account_role:
        logger.info(f"Assuming cross-account role: {cross_account_role}")
        assumed_role = sts_client.assume_role(
            RoleArn=cross_account_role,
            RoleSessionName='QScannerSession'
        )

        lambda_client_temp = boto3.client(
            'lambda',
            aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
            aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
            aws_session_token=assumed_role['Credentials']['SessionToken']
        )
    else:
        lambda_client_temp = lambda_client

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


def run_qscanner(function_arn: str, qualys_creds: Dict[str, str], aws_region: str) -> Dict[str, Any]:
    logger.info(f"Starting QScanner for Lambda function: {function_arn}")

    cmd = [
        QSCANNER_PATH,
        '--pod', qualys_creds['qualys_pod'],
        '--access-token', qualys_creds['qualys_access_token'],
        '--output-format', 'json',
        'lambda', function_arn
    ]

    env = os.environ.copy()
    env['AWS_REGION'] = aws_region

    if 'registry_username' in qualys_creds:
        env['QSCANNER_REGISTRY_USERNAME'] = qualys_creds['registry_username']
    if 'registry_password' in qualys_creds:
        env['QSCANNER_REGISTRY_PASSWORD'] = qualys_creds['registry_password']
    if 'registry_token' in qualys_creds:
        env['QSCANNER_REGISTRY_TOKEN'] = qualys_creds['registry_token']

    logger.info(f"Executing: {' '.join(cmd[:6])} [credentials hidden] lambda {function_arn}")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT
        )

        if result.returncode != 0:
            logger.error(f"QScanner failed with exit code {result.returncode}")
            logger.error(f"STDOUT: {result.stdout}")
            logger.error(f"STDERR: {result.stderr}")
            raise ScanException(f"QScanner failed: {result.stderr}")

        logger.info("QScanner completed successfully")
        logger.info(f"STDOUT: {result.stdout}")

        try:
            scan_results = json.loads(result.stdout) if result.stdout else {}
        except json.JSONDecodeError:
            logger.warning("Failed to parse QScanner output as JSON")
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


def store_results(lambda_details: Dict[str, Any], scan_results: Dict[str, Any]) -> None:
    timestamp = datetime.utcnow().isoformat()

    full_results = {
        'scan_timestamp': timestamp,
        'lambda_function': lambda_details,
        'scan_results': scan_results
    }

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

    if SNS_TOPIC_ARN:
        try:
            message = {
                'function_name': lambda_details['function_name'],
                'function_arn': lambda_details['function_arn'],
                'scan_timestamp': timestamp,
                'scan_success': scan_results['success'],
                'image_uri': lambda_details.get('image_uri', 'N/A')
            }

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
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        if 'detail' not in event:
            raise ValueError("Invalid event structure: missing 'detail' field")

        detail = event['detail']

        if 'responseElements' in detail and detail['responseElements']:
            function_arn = detail['responseElements'].get('functionArn')
        elif 'requestParameters' in detail:
            function_name = detail['requestParameters'].get('functionName')
            if function_name:
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

        qualys_creds = get_qualys_credentials()
        cross_account_role = os.environ.get('CROSS_ACCOUNT_ROLE_ARN')
        lambda_details = get_lambda_details(function_arn, cross_account_role)

        code_sha256 = lambda_details.get('code_sha256')
        if code_sha256 and check_scan_cache(function_arn, code_sha256):
            logger.info(f"Skipping scan - already scanned recently")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Scan skipped - cache hit',
                    'function_arn': function_arn,
                    'code_sha256': code_sha256
                })
            }

        aws_region = event.get('region', os.environ.get('AWS_REGION', 'us-east-1'))

        logger.info(f"Scanning Lambda: {function_arn}")
        logger.info(f"Package type: {lambda_details['package_type']}, Code SHA256: {code_sha256}")

        scan_results = run_qscanner(function_arn, qualys_creds, aws_region)

        update_scan_cache(function_arn, lambda_details, scan_results)
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

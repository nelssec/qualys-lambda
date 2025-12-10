import os
import json
import boto3
import subprocess
import logging
import re
import glob
import time
import random
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, Any, Optional, Callable
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)

lambda_client = boto3.client('lambda')
secrets_manager = boto3.client('secretsmanager')
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')
sts_client = boto3.client('sts')
cloudwatch = boto3.client('cloudwatch')
dynamodb = boto3.resource('dynamodb')

QUALYS_SECRET_ARN = os.environ.get('QUALYS_SECRET_ARN')
RESULTS_S3_BUCKET = os.environ.get('RESULTS_S3_BUCKET')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
SCAN_CACHE_TABLE = os.environ.get('SCAN_CACHE_TABLE')

try:
    SCAN_TIMEOUT = int(os.environ.get('SCAN_TIMEOUT', '300'))
except ValueError:
    logger.warning("Invalid SCAN_TIMEOUT, using default 300")
    SCAN_TIMEOUT = 300

try:
    CACHE_TTL_DAYS = int(os.environ.get('CACHE_TTL_DAYS', '30'))
except ValueError:
    logger.warning("Invalid CACHE_TTL_DAYS, using default 30")
    CACHE_TTL_DAYS = 30

QSCANNER_PATH = os.environ.get('QSCANNER_PATH', '/opt/bin/qscanner')
if not QSCANNER_PATH:
    QSCANNER_PATH = '/opt/bin/qscanner'

SCANNER_EXTERNAL_ID = os.environ.get('SCANNER_EXTERNAL_ID')
ENABLE_TAGGING = os.environ.get('ENABLE_TAGGING', 'true').lower() == 'true'


class ScanException(Exception):
    pass


def validate_pod(pod: str) -> bool:
    return bool(re.match(r'^[A-Z0-9]+$', pod))


def validate_access_token(token: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9_.-]{20,1000}$', token))


def validate_function_arn(arn: str) -> bool:
    pattern = r'^arn:aws:lambda:[a-z0-9-]+:\d{12}:function:[a-zA-Z0-9-_]{1,64}$'
    return bool(re.match(pattern, arn))


def validate_function_name(name: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9-_]{1,64}$', name))


def validate_role_arn(arn: str) -> bool:
    if not arn or not isinstance(arn, str):
        return False
    return bool(re.match(r'^arn:aws:iam::\d{12}:role/[a-zA-Z0-9+=,.@_-]{1,64}$', arn))


def sanitize_log_output(output: str) -> str:
    if not output:
        return ""
    output = re.sub(r'[a-zA-Z0-9]{32,}', '[REDACTED]', output)
    output = re.sub(r'(token|password|secret|key)[\s:=]+\S+', r'\1=[REDACTED]', output, flags=re.IGNORECASE)
    return output


def publish_custom_metrics(metric_data: Dict[str, Any]) -> None:
    try:
        metrics = []
        namespace = 'QualysLambdaScanner'

        if 'scan_success' in metric_data:
            metrics.append({
                'MetricName': 'ScanSuccess',
                'Value': 1 if metric_data['scan_success'] else 0,
                'Unit': 'Count'
            })

        if 'scan_partial' in metric_data:
            metrics.append({
                'MetricName': 'ScanPartialSuccess',
                'Value': 1 if metric_data['scan_partial'] else 0,
                'Unit': 'Count'
            })

        if 'scan_duration' in metric_data:
            metrics.append({
                'MetricName': 'ScanDuration',
                'Value': metric_data['scan_duration'],
                'Unit': 'Seconds'
            })

        if 'cache_hit' in metric_data:
            metrics.append({
                'MetricName': 'CacheHit',
                'Value': 1 if metric_data['cache_hit'] else 0,
                'Unit': 'Count'
            })

        if 'vulnerability_count' in metric_data:
            metrics.append({
                'MetricName': 'VulnerabilityCount',
                'Value': metric_data['vulnerability_count'],
                'Unit': 'Count'
            })

        if metrics:
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=metrics)

    except Exception as e:
        logger.error(f"Failed to publish metrics: {e}")


def aws_retry(max_retries: int = 5, initial_delay: float = 0.5, max_delay: float = 30):
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    retryable_codes = [
                        'Throttling', 'ThrottlingException', 'RequestThrottled',
                        'ProvisionedThroughputExceededException', 'ServiceUnavailable',
                        'InternalError', 'InternalServiceError', 'RequestLimitExceeded',
                        'TooManyRequestsException', 'TransactionConflictException'
                    ]
                    if error_code in retryable_codes and attempt < max_retries - 1:
                        delay = min(initial_delay * (2 ** attempt), max_delay)
                        delay = delay * (0.5 + random.random())
                        logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {error_code}")
                        time.sleep(delay)
                        last_exception = e
                    else:
                        raise
                except BotoCoreError as e:
                    if attempt < max_retries - 1:
                        delay = min(initial_delay * (2 ** attempt), max_delay)
                        delay = delay * (0.5 + random.random())
                        logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {type(e).__name__}")
                        time.sleep(delay)
                        last_exception = e
                    else:
                        raise
            if last_exception:
                raise last_exception
            raise ScanException(f"Max retries exceeded for {func.__name__}")
        return wrapper
    return decorator


@aws_retry(max_retries=5, initial_delay=0.5)
def get_qualys_credentials() -> Dict[str, str]:
    response = secrets_manager.get_secret_value(SecretId=QUALYS_SECRET_ARN)
    secret = json.loads(response['SecretString'])

    required_fields = ['qualys_pod', 'qualys_access_token']
    for field in required_fields:
        if field not in secret:
            raise ValueError(f"Missing required field: {field}")

    if not validate_pod(secret['qualys_pod']):
        raise ValueError("Invalid POD format")

    if not validate_access_token(secret['qualys_access_token']):
        raise ValueError("Invalid access token format")

    return secret


@aws_retry(max_retries=5, initial_delay=0.5)
def _get_cache_item(table, function_arn: str) -> Optional[Dict]:
    response = table.get_item(Key={'function_arn': function_arn})
    return response.get('Item')


def check_scan_cache(function_arn: str, code_sha256: str) -> bool:
    if not SCAN_CACHE_TABLE or not code_sha256:
        return False

    try:
        table = dynamodb.Table(SCAN_CACHE_TABLE)
        item = _get_cache_item(table, function_arn)

        if not item:
            return False

        cached_sha256 = item.get('code_sha256')
        scan_timestamp = item.get('scan_timestamp')

        if cached_sha256 != code_sha256:
            return False

        if scan_timestamp:
            scan_time = datetime.fromisoformat(scan_timestamp)
            cache_expiry = scan_time + timedelta(days=CACHE_TTL_DAYS)
            if datetime.utcnow() > cache_expiry:
                return False

        return True

    except Exception as e:
        logger.error(f"Cache check error: {e}")
        return False


@aws_retry(max_retries=5, initial_delay=0.5)
def _put_cache_item(table, item: Dict) -> None:
    table.put_item(Item=item)


def update_scan_cache(function_arn: str, lambda_details: Dict[str, Any], scan_results: Dict[str, Any]) -> None:
    if not SCAN_CACHE_TABLE:
        return

    try:
        table = dynamodb.Table(SCAN_CACHE_TABLE)
        timestamp = datetime.utcnow()

        item = {
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

        _put_cache_item(table, item)

    except Exception as e:
        logger.error(f"Cache update error: {e}")


@aws_retry(max_retries=5, initial_delay=0.5)
def _assume_role(role_arn: str, session_name: str, external_id: str) -> Dict:
    return sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        DurationSeconds=900,
        ExternalId=external_id
    )


@aws_retry(max_retries=5, initial_delay=0.5)
def _get_lambda_function(client, function_arn: str) -> Dict:
    return client.get_function(FunctionName=function_arn)


def get_target_lambda_client(cross_account_role: Optional[str] = None) -> Any:
    if cross_account_role:
        if not validate_role_arn(cross_account_role):
            raise ValueError(f"Invalid cross-account role ARN format")

        assumed_role = _assume_role(
            cross_account_role,
            'QScannerSession',
            SCANNER_EXTERNAL_ID
        )

        return boto3.client(
            'lambda',
            aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
            aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
            aws_session_token=assumed_role['Credentials']['SessionToken']
        )
    else:
        return lambda_client


def get_lambda_details(function_arn: str, target_lambda_client: Optional[Any] = None) -> Dict[str, Any]:
    client = target_lambda_client if target_lambda_client else lambda_client
    response = _get_lambda_function(client, function_arn)
    config = response['Configuration']

    return {
        'function_name': config['FunctionName'],
        'function_arn': config['FunctionArn'],
        'runtime': config.get('Runtime', 'N/A'),
        'package_type': config.get('PackageType', 'Zip'),
        'code_sha256': config.get('CodeSha256'),
        'image_uri': config.get('ImageUri'),
        'last_modified': config.get('LastModified'),
        'code_size': config.get('CodeSize'),
        'memory_size': config.get('MemorySize'),
        'timeout': config.get('Timeout'),
    }


def run_qscanner(lambda_details: Dict[str, Any], qualys_creds: Dict[str, str], aws_region: str) -> Dict[str, Any]:
    package_type = lambda_details.get('package_type', 'Zip')
    function_arn = lambda_details['function_arn']
    image_uri = lambda_details.get('image_uri')

    base_cmd = [
        QSCANNER_PATH,
        '--pod', qualys_creds['qualys_pod'],
        '--access-token', qualys_creds['qualys_access_token'],
        '--output-dir', '/tmp/qscanner-output',
        '--cache-dir', '/tmp/qscanner-cache',
        '--scan-types', 'pkg,secret',
    ]

    if package_type == 'Image' and image_uri:
        cmd = base_cmd + ['image', image_uri]
    else:
        cmd = base_cmd + ['lambda', function_arn]

    env = os.environ.copy()
    env['AWS_REGION'] = aws_region

    if 'registry_username' in qualys_creds:
        env['QSCANNER_REGISTRY_USERNAME'] = qualys_creds['registry_username']
    if 'registry_password' in qualys_creds:
        env['QSCANNER_REGISTRY_PASSWORD'] = qualys_creds['registry_password']
    if 'registry_token' in qualys_creds:
        env['QSCANNER_REGISTRY_TOKEN'] = qualys_creds['registry_token']

    PARTIAL_SUCCESS_EXIT_CODES = {40}

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT
        )

        if result.returncode != 0:
            if result.returncode in PARTIAL_SUCCESS_EXIT_CODES:
                logger.warning(f"QScanner partial success: exit code {result.returncode}")
            else:
                logger.error(f"QScanner failed: exit code {result.returncode}")
                logger.error(f"stderr: {sanitize_log_output(result.stderr)}")
                raise ScanException("QScanner execution failed")

        scan_results = {}
        output_dir = '/tmp/qscanner-output'

        try:
            scan_result_files = glob.glob(f'{output_dir}/*-ScanResult.json')
            if scan_result_files:
                with open(scan_result_files[0], 'r') as f:
                    scan_results = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read scan results: {e}")

        return {
            'success': True,
            'partial': result.returncode in PARTIAL_SUCCESS_EXIT_CODES,
            'exit_code': result.returncode,
            'results': scan_results,
            'stdout': result.stdout,
            'stderr': result.stderr
        }

    except subprocess.TimeoutExpired:
        raise ScanException(f"Scan timeout after {SCAN_TIMEOUT} seconds")


def tag_lambda_function(
    function_arn: str,
    scan_timestamp: str,
    scan_success: bool,
    scan_partial: bool = False,
    target_lambda_client: Optional[Any] = None
) -> None:
    try:
        if scan_success and scan_partial:
            status = 'partial'
        elif scan_success:
            status = 'success'
        else:
            status = 'failed'

        tags = {
            'QualysScanTimestamp': scan_timestamp,
            'QualysScanStatus': status
        }

        client = target_lambda_client if target_lambda_client else lambda_client
        client.tag_resource(Resource=function_arn, Tags=tags)

    except Exception as e:
        logger.error(f"Failed to tag Lambda: {e}")


@aws_retry(max_retries=5, initial_delay=0.5)
def _s3_put_object(bucket: str, key: str, body: str) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType='application/json'
    )


@aws_retry(max_retries=5, initial_delay=0.5)
def _sns_publish(topic_arn: str, subject: str, message: str) -> None:
    sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)


def store_results(
    lambda_details: Dict[str, Any],
    scan_results: Dict[str, Any],
    target_lambda_client: Optional[Any] = None
) -> None:
    timestamp = datetime.utcnow().isoformat()

    full_results = {
        'scan_timestamp': timestamp,
        'lambda_function': lambda_details,
        'scan_results': scan_results
    }

    if RESULTS_S3_BUCKET:
        try:
            key = f"scans/{lambda_details['function_name']}/{timestamp}.json"
            _s3_put_object(RESULTS_S3_BUCKET, key, json.dumps(full_results, indent=2))
        except Exception as e:
            logger.error(f"S3 storage error: {e}")

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

            _sns_publish(
                SNS_TOPIC_ARN,
                f"QScanner Results: {lambda_details['function_name']}",
                json.dumps(message, indent=2)
            )
        except Exception as e:
            logger.error(f"SNS publish error: {e}")

    if ENABLE_TAGGING:
        tag_lambda_function(
            lambda_details['function_arn'],
            timestamp,
            scan_results['success'],
            scan_results.get('partial', False),
            target_lambda_client
        )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        if 'detail' not in event:
            raise ValueError("Invalid event structure")

        detail = event['detail']

        if 'responseElements' in detail and detail['responseElements']:
            function_arn = detail['responseElements'].get('functionArn')
        elif 'requestParameters' in detail:
            function_name = detail['requestParameters'].get('functionName')
            if function_name and not validate_function_name(function_name):
                raise ValueError("Invalid function name format")

            if function_name:
                account_id = event.get('account', detail.get('userIdentity', {}).get('accountId'))
                region = event.get('region', 'us-east-1')
                function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
            else:
                raise ValueError("Could not extract function name")
        else:
            raise ValueError("Could not extract function ARN")

        if not function_arn or not validate_function_arn(function_arn):
            raise ValueError("Invalid function ARN")

        scanner_function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
        target_function_name = function_arn.split(':')[-1]

        if scanner_function_name and target_function_name == scanner_function_name:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Skipped self-scan',
                    'function_arn': function_arn
                })
            }

        qualys_creds = get_qualys_credentials()
        cross_account_role = os.environ.get('CROSS_ACCOUNT_ROLE_ARN')

        target_lambda_client = get_target_lambda_client(cross_account_role)
        lambda_details = get_lambda_details(function_arn, target_lambda_client)

        code_sha256 = lambda_details.get('code_sha256')
        if code_sha256 and check_scan_cache(function_arn, code_sha256):
            publish_custom_metrics({'cache_hit': True})
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Cache hit',
                    'function_arn': function_arn
                })
            }

        aws_region = event.get('region', os.environ.get('AWS_REGION', 'us-east-1'))

        scan_start_time = time.time()
        scan_results = run_qscanner(lambda_details, qualys_creds, aws_region)
        scan_duration = time.time() - scan_start_time

        update_scan_cache(function_arn, lambda_details, scan_results)
        store_results(lambda_details, scan_results, target_lambda_client)

        vuln_count = 0
        if 'results' in scan_results and isinstance(scan_results['results'], dict):
            vuln_summary = scan_results['results'].get('vulnerabilities', {})
            if isinstance(vuln_summary, dict):
                vuln_count = sum(vuln_summary.values()) if vuln_summary else 0
            elif isinstance(vuln_summary, list):
                vuln_count = len(vuln_summary)

        publish_custom_metrics({
            'cache_hit': False,
            'scan_success': scan_results['success'],
            'scan_partial': scan_results.get('partial', False),
            'scan_duration': scan_duration,
            'vulnerability_count': vuln_count
        })

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Scan completed',
                'function_arn': function_arn,
                'package_type': lambda_details['package_type'],
                'scan_success': scan_results['success'],
                'scan_partial': scan_results.get('partial', False)
            })
        }

    except ScanException as e:
        logger.error(f"Scan failed: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Scan failed',
                'request_id': context.aws_request_id
            })
        }

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Internal error',
                'request_id': context.aws_request_id
            })
        }

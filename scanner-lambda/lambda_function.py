import os
import json
import boto3
import subprocess
import logging
import re
import glob
import time
import random
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, Any, Optional, Callable, Tuple
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Suppress noisy boto/botocore logs
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
    logger.warning("Invalid SCAN_TIMEOUT environment variable, using default 300")
    SCAN_TIMEOUT = 300

try:
    CACHE_TTL_DAYS = int(os.environ.get('CACHE_TTL_DAYS', '30'))
except ValueError:
    logger.warning("Invalid CACHE_TTL_DAYS environment variable, using default 30")
    CACHE_TTL_DAYS = 30

QSCANNER_PATH = os.environ.get('QSCANNER_PATH', '/opt/bin/qscanner')
if not QSCANNER_PATH:
    logger.error("QSCANNER_PATH is empty, using default /opt/bin/qscanner")
    QSCANNER_PATH = '/opt/bin/qscanner'

SCANNER_EXTERNAL_ID = os.environ.get('SCANNER_EXTERNAL_ID')
if not SCANNER_EXTERNAL_ID:
    logger.warning("SCANNER_EXTERNAL_ID not set - cross-account scanning will fail")

ENABLE_TAGGING = os.environ.get('ENABLE_TAGGING', 'true').lower() == 'true'

# Qualys Pod to Gateway URL mapping
# Gateway URLs for Container Security API (csapi)
QUALYS_GATEWAY_MAP = {
    'US1': 'https://gateway.qg1.apps.qualys.com',
    'US2': 'https://gateway.qg2.apps.qualys.com',
    'US3': 'https://gateway.qg3.apps.qualys.com',
    'US4': 'https://gateway.qg4.apps.qualys.com',
    'GOV1': 'https://gateway.qg1.apps.qualys.com',
    'EU1': 'https://gateway.qg1.apps.qualys.eu',
    'EU2': 'https://gateway.qg2.apps.qualys.eu',
    'EU3': 'https://gateway.qg3.apps.qualys.it',
    'IN1': 'https://gateway.qg1.apps.qualys.in',
    'CA1': 'https://gateway.qg1.apps.qualys.ca',
    'AE1': 'https://gateway.qg1.apps.qualys.ae',
    'UK1': 'https://gateway.qg1.apps.qualys.co.uk',
    'AU1': 'https://gateway.qg1.apps.qualys.com.au',
    'KSA1': 'https://gateway.qg1.apps.qualysksa.com',
}


class ScanException(Exception):
    pass


def validate_pod(pod: str) -> bool:
    """Validate Qualys POD name format"""
    return bool(re.match(r'^[A-Z0-9]+$', pod))


def validate_access_token(token: str) -> bool:
    """Validate Qualys access token format (supports JWT tokens)"""
    return bool(re.match(r'^[a-zA-Z0-9_.-]{20,1000}$', token))


def validate_function_arn(arn: str) -> bool:
    """Validate Lambda function ARN format"""
    pattern = r'^arn:aws:lambda:[a-z0-9-]+:\d{12}:function:[a-zA-Z0-9-_]{1,64}$'
    return bool(re.match(pattern, arn))


def validate_function_name(name: str) -> bool:
    """Validate Lambda function name"""
    pattern = r'^[a-zA-Z0-9-_]{1,64}$'
    return bool(re.match(pattern, name))


def validate_tag_value(value: str) -> bool:
    """Validate AWS Lambda tag value format

    AWS tag value constraints:
    - Max length: 256 characters
    - Allowed characters: a-z, A-Z, 0-9, spaces, and + - = . _ : / @
    """
    if not value or not isinstance(value, str):
        return False

    if len(value) > 256:
        return False

    pattern = r'^[a-zA-Z0-9 +\-=._:/@]+$'
    return bool(re.match(pattern, value))


def validate_role_arn(arn: str) -> bool:
    """Validate IAM role ARN format for cross-account role assumption

    Expected format: arn:aws:iam::<account-id>:role/<role-name>
    """
    if not arn or not isinstance(arn, str):
        return False

    # Strict pattern for IAM role ARN
    pattern = r'^arn:aws:iam::\d{12}:role/[a-zA-Z0-9+=,.@_-]{1,64}$'
    return bool(re.match(pattern, arn))


def sanitize_log_output(output: str) -> str:
    """Remove potential secrets from log output"""
    if not output:
        return ""
    output = re.sub(r'[a-zA-Z0-9]{32,}', '[REDACTED]', output)
    output = re.sub(r'(token|password|secret|key)[\s:=]+\S+', r'\1=[REDACTED]', output, flags=re.IGNORECASE)
    return output


# =============================================================================
# Qualys Container Security API Client
# =============================================================================

def get_qualys_gateway_url(pod: str) -> str:
    """Get the Qualys gateway URL for Container Security API."""
    pod_upper = pod.upper()
    if pod_upper not in QUALYS_GATEWAY_MAP:
        logger.warning(f"Unknown Qualys pod: {pod}, defaulting to US2")
        return QUALYS_GATEWAY_MAP['US2']
    return QUALYS_GATEWAY_MAP[pod_upper]


def qualys_cs_api_request(
    gateway_url: str,
    endpoint: str,
    token: str,
    method: str = 'GET',
    data: Optional[Dict] = None,
    timeout: int = 30
) -> Tuple[int, Optional[Dict]]:
    """Make a request to the Qualys Container Security API with Bearer token auth."""
    url = f"{gateway_url}{endpoint}"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    try:
        body = json.dumps(data).encode('utf-8') if data else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode('utf-8')
            if response_body:
                return response.status, json.loads(response_body)
            return response.status, None

    except urllib.error.HTTPError as e:
        logger.error(f"Qualys CS API HTTP error: {e.code} - {e.reason}")
        try:
            error_body = e.read().decode('utf-8')
            logger.error(f"CS API error body: {error_body[:500]}")
            # Return the error body for caller to inspect
            if error_body:
                return e.code, json.loads(error_body) if error_body.strip().startswith('{') else {'error': error_body}
        except Exception:
            pass
        return e.code, None
    except urllib.error.URLError as e:
        logger.error(f"Qualys CS API URL error: {e.reason}")
        return 0, None
    except Exception as e:
        logger.error(f"Qualys CS API request failed: {e}")
        return 0, None


def get_image_by_sha(gateway_url: str, token: str, image_sha: str) -> Optional[Dict]:
    """Get image details from Qualys CS by SHA256 using Bearer token auth."""
    # Remove 'sha256:' prefix if present
    if image_sha.startswith('sha256:'):
        image_sha = image_sha[7:]

    endpoint = f"/csapi/v1.3/images/{image_sha}"
    status, response = qualys_cs_api_request(gateway_url, endpoint, token)

    if status == 200 and response:
        return response
    elif status == 404:
        pass  # Not found is expected during retries
    elif status != 0:
        logger.error(f"Qualys CS API error: status={status}")

    return None


def publish_custom_metrics(metric_data: Dict[str, Any]) -> None:
    """Publish custom CloudWatch metrics for scan statistics"""
    try:
        metrics = []
        namespace = 'QualysLambdaScanner'

        # Scan success/failure metric
        if 'scan_success' in metric_data:
            metrics.append({
                'MetricName': 'ScanSuccess',
                'Value': 1 if metric_data['scan_success'] else 0,
                'Unit': 'Count'
            })

        # Partial success metric
        if 'scan_partial' in metric_data:
            metrics.append({
                'MetricName': 'ScanPartialSuccess',
                'Value': 1 if metric_data['scan_partial'] else 0,
                'Unit': 'Count'
            })

        # Scan duration metric
        if 'scan_duration' in metric_data:
            metrics.append({
                'MetricName': 'ScanDuration',
                'Value': metric_data['scan_duration'],
                'Unit': 'Seconds'
            })

        # Cache hit rate metric
        if 'cache_hit' in metric_data:
            metrics.append({
                'MetricName': 'CacheHit',
                'Value': 1 if metric_data['cache_hit'] else 0,
                'Unit': 'Count'
            })

        # Vulnerability count metric
        if 'vulnerability_count' in metric_data:
            metrics.append({
                'MetricName': 'VulnerabilityCount',
                'Value': metric_data['vulnerability_count'],
                'Unit': 'Count'
            })

        if metrics:
            cloudwatch.put_metric_data(
                Namespace=namespace,
                MetricData=metrics
            )
            logger.info(f"Published {len(metrics)} custom metrics to CloudWatch")

    except Exception as e:
        logger.error(f"Failed to publish custom metrics: {e}")


def aws_retry(max_retries: int = 5, initial_delay: float = 0.5, max_delay: float = 30):
    """Decorator for retrying AWS API calls with exponential backoff and jitter."""
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
                        logger.warning(f"AWS API {func.__name__} attempt {attempt + 1}/{max_retries} failed with {error_code}, retrying in {delay:.1f}s")
                        time.sleep(delay)
                        last_exception = e
                    else:
                        raise
                except BotoCoreError as e:
                    if attempt < max_retries - 1:
                        delay = min(initial_delay * (2 ** attempt), max_delay)
                        delay = delay * (0.5 + random.random())
                        logger.warning(f"AWS API {func.__name__} attempt {attempt + 1}/{max_retries} failed with {type(e).__name__}, retrying in {delay:.1f}s")
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
    """Retrieve Qualys credentials from Secrets Manager with retry."""
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

    logger.info(f"Retrieved Qualys credentials for pod: {secret['qualys_pod']}")
    return secret


@aws_retry(max_retries=5, initial_delay=0.5)
def _get_cache_item(table, function_arn: str) -> Optional[Dict]:
    """Get item from DynamoDB cache with retry."""
    response = table.get_item(Key={'function_arn': function_arn})
    return response.get('Item')


def check_scan_cache(function_arn: str, code_sha256: str) -> bool:
    """Check if function has been scanned recently with same code hash."""
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


@aws_retry(max_retries=5, initial_delay=0.5)
def _put_cache_item(table, item: Dict) -> None:
    """Put item to DynamoDB cache with retry."""
    table.put_item(Item=item)


def update_scan_cache(function_arn: str, lambda_details: Dict[str, Any], scan_results: Dict[str, Any]) -> None:
    """Update scan cache with latest scan results."""
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
        logger.info(f"Updated scan cache for {function_arn}")

    except Exception as e:
        logger.error(f"Failed to update scan cache: {e}")


@aws_retry(max_retries=5, initial_delay=0.5)
def _assume_role(role_arn: str, session_name: str, external_id: str) -> Dict:
    """Assume IAM role with retry."""
    return sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        DurationSeconds=900,
        ExternalId=external_id
    )


@aws_retry(max_retries=5, initial_delay=0.5)
def _get_lambda_function(client, function_arn: str) -> Dict:
    """Get Lambda function details with retry."""
    return client.get_function(FunctionName=function_arn)


def get_target_lambda_client(cross_account_role: Optional[str] = None) -> Any:
    """Get Lambda client for target account.

    For standalone mode (no cross-account role), returns the default Lambda client.
    For hub-and-spoke mode (cross-account role provided), assumes the role and
    returns a client with the assumed credentials.

    Args:
        cross_account_role: Optional IAM role ARN to assume for cross-account access

    Returns:
        boto3 Lambda client for the target account
    """
    if cross_account_role:
        # Validate cross-account role ARN before attempting to assume it
        if not validate_role_arn(cross_account_role):
            raise ValueError(f"Invalid cross-account role ARN format: {cross_account_role[:50]}...")

        logger.info(f"Assuming cross-account role: {cross_account_role}")
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
    """Get Lambda function details using the provided Lambda client.

    Args:
        function_arn: ARN of the Lambda function
        target_lambda_client: Lambda client to use. If not provided, uses the default client.

    Returns:
        Dictionary with Lambda function details
    """
    client = target_lambda_client if target_lambda_client else lambda_client

    response = _get_lambda_function(client, function_arn)
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


def retry_with_backoff(func, max_retries=5, initial_delay=1, max_delay=30, jitter=True):
    """Retry a function with exponential backoff and jitter."""
    for attempt in range(max_retries):
        try:
            return func()
        except subprocess.CalledProcessError as e:
            # Only retry on specific exit codes that indicate transient failures
            if attempt < max_retries - 1 and e.returncode in [1, 2, 124, 137]:
                delay = min(initial_delay * (2 ** attempt), max_delay)
                if jitter:
                    delay = delay * (0.5 + random.random())  # 50-150% of calculated delay
                logger.warning(f"Attempt {attempt + 1}/{max_retries} failed with exit code {e.returncode}, retrying in {delay:.1f}s...")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            error_str = str(e).lower()
            is_transient = any(keyword in error_str for keyword in [
                'timeout', 'connection', 'network', 'throttl', 'rate limit',
                'service unavailable', 'internal error', 'try again'
            ])
            if attempt < max_retries - 1 and is_transient:
                delay = min(initial_delay * (2 ** attempt), max_delay)
                if jitter:
                    delay = delay * (0.5 + random.random())
                logger.warning(f"Attempt {attempt + 1}/{max_retries} failed with transient error, retrying in {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                raise
    raise ScanException("Max retries exceeded")


def run_qscanner(function_arn: str, qualys_creds: Dict[str, str], aws_region: str) -> Dict[str, Any]:
    logger.info(f"Starting QScanner for Lambda function: {function_arn}")

    cmd = [
        QSCANNER_PATH,
        '--pod', qualys_creds['qualys_pod'],
        '--access-token', qualys_creds['qualys_access_token'],
        '--output-dir', '/tmp/qscanner-output',
        '--cache-dir', '/tmp/qscanner-cache',
        '--scan-types', 'pkg,secret',
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

    # Exit codes that indicate partial success
    # 40 = Vulnerability reporter failed - scan data was still uploaded
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
                logger.warning(f"QScanner partial success with exit code {result.returncode}")
                logger.warning(f"STDOUT: {sanitize_log_output(result.stdout)}")
                logger.warning(f"STDERR: {sanitize_log_output(result.stderr)}")
                # Continue to process results - SBOM was uploaded, only vuln report failed
            else:
                logger.error(f"QScanner failed with exit code {result.returncode}")
                logger.error(f"STDOUT: {sanitize_log_output(result.stdout)}")
                logger.error(f"STDERR: {sanitize_log_output(result.stderr)}")
                raise ScanException("QScanner execution failed")
        else:
            logger.info("QScanner completed successfully")

        # Read QScanner output files from /tmp/qscanner-output/
        scan_results = {}
        output_dir = '/tmp/qscanner-output'

        try:
            # Look for *-ScanResult.json file
            import glob
            scan_result_files = glob.glob(f'{output_dir}/*-ScanResult.json')

            if scan_result_files:
                scan_result_file = scan_result_files[0]
                logger.info(f"Reading scan results from: {scan_result_file}")

                with open(scan_result_file, 'r') as f:
                    scan_results = json.load(f)
            else:
                logger.warning("No ScanResult.json file found in output directory")
                scan_results = {}

        except Exception as e:
            logger.warning(f"Failed to read QScanner output files: {e}")
            scan_results = {}

        is_partial = result.returncode in PARTIAL_SUCCESS_EXIT_CODES
        return {
            'success': True,
            'partial': is_partial,  # True if vuln report failed but SBOM succeeded
            'exit_code': result.returncode,
            'results': scan_results,
            'stdout': result.stdout,
            'stderr': result.stderr
        }

    except subprocess.TimeoutExpired:
        logger.error(f"QScanner timed out after {SCAN_TIMEOUT} seconds")
        raise ScanException(f"Scan timeout after {SCAN_TIMEOUT} seconds")


def tag_lambda_function(
    function_arn: str,
    scan_timestamp: str,
    scan_success: bool,
    scan_partial: bool = False,
    target_lambda_client: Optional[Any] = None
) -> None:
    """Tag Lambda function with scan results.

    Args:
        function_arn: ARN of the Lambda function to tag
        scan_timestamp: ISO timestamp of when scan occurred
        scan_success: Whether the scan succeeded
        scan_partial: Whether this was a partial success (SBOM uploaded but vuln report failed)
        target_lambda_client: Optional Lambda client for cross-account tagging.
                              If not provided, uses the default client (for same-account).
    """
    try:
        if scan_success and scan_partial:
            status = 'partial'  # SBOM uploaded but vuln report failed
        elif scan_success:
            status = 'success'
        else:
            status = 'failed'

        tags = {
            'QualysScanTimestamp': scan_timestamp,
            'QualysScanStatus': status
        }

        # Use provided client for cross-account, or default for same-account
        client = target_lambda_client if target_lambda_client else lambda_client
        client.tag_resource(
            Resource=function_arn,
            Tags=tags
        )

        logger.info(f"Successfully tagged Lambda function: {function_arn}")
    except Exception as e:
        logger.error(f"Failed to tag Lambda function: {e}")


@aws_retry(max_retries=5, initial_delay=0.5)
def _s3_put_object(bucket: str, key: str, body: str) -> None:
    """Put object to S3 with retry."""
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType='application/json'
    )


@aws_retry(max_retries=5, initial_delay=0.5)
def _sns_publish(topic_arn: str, subject: str, message: str) -> None:
    """Publish to SNS with retry."""
    sns_client.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=message
    )


def store_results(
    lambda_details: Dict[str, Any],
    scan_results: Dict[str, Any],
    target_lambda_client: Optional[Any] = None
) -> None:
    """Store scan results to S3 and send SNS notification.

    Args:
        lambda_details: Details about the scanned Lambda function
        scan_results: Results from the QScanner scan
        target_lambda_client: Optional Lambda client for cross-account tagging.
                              If not provided, uses the default client (for same-account).
    """
    timestamp = datetime.utcnow().isoformat()

    full_results = {
        'scan_timestamp': timestamp,
        'lambda_function': lambda_details,
        'scan_results': scan_results
    }

    if RESULTS_S3_BUCKET:
        try:
            key = f"scans/{lambda_details['function_name']}/{timestamp}.json"
            _s3_put_object(
                RESULTS_S3_BUCKET,
                key,
                json.dumps(full_results, indent=2)
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

            _sns_publish(
                SNS_TOPIC_ARN,
                f"QScanner Results: {lambda_details['function_name']}",
                json.dumps(message, indent=2)
            )
            logger.info(f"Sent notification to SNS: {SNS_TOPIC_ARN}")
        except Exception as e:
            logger.error(f"Failed to send SNS notification: {e}")

    if ENABLE_TAGGING:
        tag_lambda_function(
            lambda_details['function_arn'],
            timestamp,
            scan_results['success'],
            scan_results.get('partial', False),
            target_lambda_client
        )
    else:
        logger.info(f"Lambda tagging disabled - skipping tags for {lambda_details['function_arn']}")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info(f"Received event from source: {event.get('source')}, detail-type: {event.get('detail-type')}")

    try:
        if 'detail' not in event:
            raise ValueError("Invalid event structure: missing 'detail' field")

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
                raise ValueError("Could not extract function name from event")
        else:
            raise ValueError("Could not extract function ARN from event")

        if not function_arn or not validate_function_arn(function_arn):
            raise ValueError("Invalid or empty function ARN")

        logger.info(f"Processing Lambda function: {function_arn}")

        # Prevent infinite loop - skip scanning the scanner function itself
        scanner_function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
        target_function_name = function_arn.split(':')[-1]  # Extract function name from ARN

        if scanner_function_name and target_function_name == scanner_function_name:
            logger.info(f"Skipping scan - avoiding self-scan of scanner function: {scanner_function_name}")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Scan skipped - self-scan prevention',
                    'function_arn': function_arn
                })
            }

        qualys_creds = get_qualys_credentials()
        cross_account_role = os.environ.get('CROSS_ACCOUNT_ROLE_ARN')

        # Get Lambda client for target account
        target_lambda_client = get_target_lambda_client(cross_account_role)
        lambda_details = get_lambda_details(function_arn, target_lambda_client)

        code_sha256 = lambda_details.get('code_sha256')
        if code_sha256 and check_scan_cache(function_arn, code_sha256):
            logger.info(f"Skipping scan - already scanned recently")
            # Publish cache hit metric
            publish_custom_metrics({'cache_hit': True})
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

        # Track scan duration
        scan_start_time = time.time()
        scan_results = run_qscanner(function_arn, qualys_creds, aws_region)
        scan_duration = time.time() - scan_start_time

        update_scan_cache(function_arn, lambda_details, scan_results)
        store_results(lambda_details, scan_results, target_lambda_client)

        # Extract vulnerability count if available
        vuln_count = 0
        if 'results' in scan_results and isinstance(scan_results['results'], dict):
            vuln_summary = scan_results['results'].get('vulnerabilities', {})
            if isinstance(vuln_summary, dict):
                vuln_count = sum(vuln_summary.values()) if vuln_summary else 0
            elif isinstance(vuln_summary, list):
                vuln_count = len(vuln_summary)

        # Publish scan metrics
        publish_custom_metrics({
            'cache_hit': False,
            'scan_success': scan_results['success'],
            'scan_partial': scan_results.get('partial', False),
            'scan_duration': scan_duration,
            'vulnerability_count': vuln_count
        })

        is_partial = scan_results.get('partial', False)
        message = 'Scan completed with partial success (vuln report fetch failed)' if is_partial else 'Scan completed successfully'

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': message,
                'function_arn': function_arn,
                'package_type': lambda_details['package_type'],
                'scan_success': scan_results['success'],
                'scan_partial': is_partial
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
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Internal error',
                'request_id': context.aws_request_id
            })
        }

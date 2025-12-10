import boto3
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clients
lambda_client = boto3.client('lambda')
sts_client = boto3.client('sts')

# Environment variables
SCANNER_FUNCTION_NAME = os.environ.get('SCANNER_FUNCTION_NAME', '')
CROSS_ACCOUNT_ROLE_NAME = os.environ.get('CROSS_ACCOUNT_ROLE_NAME', '')
SCANNER_EXTERNAL_ID = os.environ.get('SCANNER_EXTERNAL_ID', '')
EXCLUDE_PATTERNS = os.environ.get('EXCLUDE_PATTERNS', 'qualys-lambda-scanner,bulk-scan').split(',')
INVOCATION_DELAY_MS = int(os.environ.get('INVOCATION_DELAY_MS', '100'))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '10'))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '100'))
CURRENT_REGION = os.environ.get('AWS_REGION', 'us-east-1')
DEFAULT_REGIONS = [r.strip() for r in os.environ.get('DEFAULT_REGIONS', '').split(',') if r.strip()]

# Validation patterns
ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')
REGION_PATTERN = re.compile(r'^[a-z]{2}-[a-z]+-\d+$')


def validate_account_id(account_id: str) -> bool:
    return bool(ACCOUNT_ID_PATTERN.match(account_id))


def validate_region(region: str) -> bool:
    return bool(REGION_PATTERN.match(region))


def should_exclude(function_name: str, exclude_patterns: list) -> bool:
    for pattern in exclude_patterns:
        pattern = pattern.strip()
        if pattern and pattern in function_name:
            return True
    return False


def get_lambda_client_for_region(region: str) -> Optional[boto3.client]:
    if not validate_region(region):
        logger.error(f"Invalid region format: {region}")
        return None

    try:
        return boto3.client('lambda', region_name=region)
    except Exception as e:
        logger.error(f"Failed to create Lambda client for region {region}: {e}")
        return None


def get_lambda_client_for_account(account_id: str, region: str = None) -> Optional[boto3.client]:
    if not CROSS_ACCOUNT_ROLE_NAME:
        return None

    if not validate_account_id(account_id):
        logger.error(f"Invalid account ID format: {account_id}")
        return None

    if region and not validate_region(region):
        logger.error(f"Invalid region format: {region}")
        return None

    try:
        role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName='BulkScanSession',
            DurationSeconds=3600,
            ExternalId=SCANNER_EXTERNAL_ID
        )

        client_kwargs = {
            'aws_access_key_id': assumed_role['Credentials']['AccessKeyId'],
            'aws_secret_access_key': assumed_role['Credentials']['SecretAccessKey'],
            'aws_session_token': assumed_role['Credentials']['SessionToken']
        }
        if region:
            client_kwargs['region_name'] = region

        return boto3.client('lambda', **client_kwargs)
    except Exception as e:
        logger.error(f"Failed to assume role in account {account_id}: {e}")
        return None


def list_all_functions(client: boto3.client, exclude_patterns: list) -> List[Dict[str, Any]]:
    functions = []
    paginator = client.get_paginator('list_functions')

    for page in paginator.paginate():
        for func in page.get('Functions', []):
            function_name = func.get('FunctionName', '')

            # Skip excluded functions
            if should_exclude(function_name, exclude_patterns):
                logger.debug(f"Excluding function: {function_name}")
                continue

            functions.append({
                'FunctionArn': func['FunctionArn'],
                'FunctionName': function_name,
                'CodeSha256': func.get('CodeSha256', ''),
                'Runtime': func.get('Runtime', 'container'),
                'PackageType': func.get('PackageType', 'Zip')
            })

    return functions


def invoke_scanner(func: Dict[str, Any], source_account: str) -> Tuple[bool, str]:
    function_name = func.get('FunctionName', 'unknown')

    if not SCANNER_FUNCTION_NAME:
        logger.error("SCANNER_FUNCTION_NAME not configured")
        return False, function_name

    # Create a synthetic CloudTrail-like event for the scanner
    scan_event = {
        'source': 'qualys.bulk-scan',
        'detail-type': 'Bulk Scan Request',
        'detail': {
            'eventName': 'BulkScanRequest',
            'eventSource': 'lambda.amazonaws.com',
            'requestParameters': {
                'functionName': func['FunctionArn']
            },
            'responseElements': {
                'functionArn': func['FunctionArn'],
                'functionName': function_name,
                'codeSha256': func['CodeSha256'],
                'runtime': func['Runtime'],
                'packageType': func['PackageType']
            },
            'userIdentity': {
                'accountId': source_account
            }
        }
    }

    try:
        lambda_client.invoke(
            FunctionName=SCANNER_FUNCTION_NAME,
            InvocationType='Event',  # Async invocation
            Payload=json.dumps(scan_event)
        )
        return True, function_name
    except Exception as e:
        logger.error(f"Failed to invoke scanner for {function_name}: {e}")
        return False, function_name


def invoke_batch_parallel(functions: List[Dict[str, Any]], account_id: str) -> Tuple[int, int]:
    invoked = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks
        future_to_func = {
            executor.submit(invoke_scanner, func, account_id): func
            for func in functions
        }

        # Collect results as they complete
        for future in as_completed(future_to_func):
            try:
                success, func_name = future.result()
                if success:
                    invoked += 1
                else:
                    failed += 1
            except Exception as e:
                func = future_to_func[future]
                logger.error(f"Exception invoking scanner for {func.get('FunctionName', 'unknown')}: {e}")
                failed += 1

    return invoked, failed


def process_region(account_id: str, region: str, current_account: str,
                    exclude_patterns: list, dry_run: bool) -> Dict[str, Any]:
    result = {
        'region': region,
        'status': 'pending',
        'functions': 0,
        'invoked': 0,
        'failed': 0
    }

    try:
        # Get appropriate Lambda client for this account/region
        if account_id == current_account:
            if region == CURRENT_REGION:
                list_client = lambda_client
            else:
                list_client = get_lambda_client_for_region(region)
        else:
            list_client = get_lambda_client_for_account(account_id, region)

        if not list_client:
            result['status'] = 'failed'
            result['error'] = 'Could not create Lambda client'
            return result

        # List all functions in this region
        functions = list_all_functions(list_client, exclude_patterns)
        function_count = len(functions)
        result['functions'] = function_count

        logger.info(f"Found {function_count} functions in {account_id}/{region}")

        if dry_run:
            result['status'] = 'dry_run'
            return result

        if function_count == 0:
            result['status'] = 'success'
            return result

        # Invoke scanner in parallel batches
        invoked = 0
        failed = 0

        for i in range(0, len(functions), BATCH_SIZE):
            batch = functions[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(functions) + BATCH_SIZE - 1) // BATCH_SIZE

            logger.info(f"Processing batch {batch_num}/{total_batches} in {region} ({len(batch)} functions)")

            batch_invoked, batch_failed = invoke_batch_parallel(batch, account_id)
            invoked += batch_invoked
            failed += batch_failed

            # Pause between batches to avoid throttling
            if i + BATCH_SIZE < len(functions) and INVOCATION_DELAY_MS > 0:
                pause_seconds = (INVOCATION_DELAY_MS * BATCH_SIZE) / 1000.0
                time.sleep(pause_seconds)

        result['invoked'] = invoked
        result['failed'] = failed
        result['status'] = 'success'

    except Exception as e:
        logger.error(f"Error processing {account_id}/{region}: {e}")
        result['status'] = 'error'
        result['error'] = str(e)

    return result


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    logger.info(f"Bulk scan triggered with event: {json.dumps(event)}")

    # Validate scanner function is configured
    if not SCANNER_FUNCTION_NAME:
        return {
            'statusCode': 500,
            'body': {'error': 'SCANNER_FUNCTION_NAME not configured'}
        }

    # Parse event
    account_ids = event.get('account_ids', [])
    event_regions = event.get('regions', [])
    dry_run = event.get('dry_run', False)
    additional_excludes = event.get('exclude_patterns', [])

    # Determine regions to scan
    if event_regions:
        regions = [r.strip() for r in event_regions if r.strip()]
    elif DEFAULT_REGIONS:
        regions = DEFAULT_REGIONS
    else:
        regions = [CURRENT_REGION]

    # Validate regions
    invalid_regions = [r for r in regions if not validate_region(r)]
    if invalid_regions:
        return {
            'statusCode': 400,
            'body': {'error': f'Invalid regions: {invalid_regions}'}
        }

    # Create local exclude patterns list (avoid modifying global for thread safety)
    exclude_patterns = list(EXCLUDE_PATTERNS) + additional_excludes

    results = {
        'accounts_processed': 0,
        'accounts_failed': 0,
        'regions_scanned': len(regions),
        'total_functions': 0,
        'invoked': 0,
        'failed': 0,
        'details': []
    }

    # Get current account ID
    current_account = sts_client.get_caller_identity()['Account']

    # If no account IDs specified, scan current account
    if not account_ids:
        account_ids = [current_account]

    logger.info(f"Scanning {len(account_ids)} account(s) across {len(regions)} region(s): {regions}")

    for account_id in account_ids:
        account_id = str(account_id).strip()

        if not validate_account_id(account_id):
            logger.error(f"Invalid account ID: {account_id}")
            results['accounts_failed'] += 1
            continue

        logger.info(f"Processing account: {account_id}")

        account_detail = {
            'account': account_id,
            'status': 'success',
            'regions': [],
            'total_functions': 0,
            'total_invoked': 0,
            'total_failed': 0
        }

        account_has_error = False

        for region in regions:
            logger.info(f"Processing region: {region}")

            region_result = process_region(
                account_id, region, current_account,
                exclude_patterns, dry_run
            )

            account_detail['regions'].append(region_result)
            account_detail['total_functions'] += region_result.get('functions', 0)
            account_detail['total_invoked'] += region_result.get('invoked', 0)
            account_detail['total_failed'] += region_result.get('failed', 0)

            results['total_functions'] += region_result.get('functions', 0)
            results['invoked'] += region_result.get('invoked', 0)
            results['failed'] += region_result.get('failed', 0)

            if region_result.get('status') == 'error':
                account_has_error = True

        if account_has_error:
            account_detail['status'] = 'partial'

        results['details'].append(account_detail)
        results['accounts_processed'] += 1

    logger.info(f"Bulk scan complete: {json.dumps(results)}")

    return {
        'statusCode': 200,
        'body': results
    }

"""
Bulk Scan Lambda - Scans all existing Lambda functions in an account.

This function is triggered manually or on a schedule to scan existing functions
that weren't caught by the event-driven scanner (CreateFunction/UpdateFunction events).

Architecture:
- Directly invokes the scanner Lambda asynchronously for each function
- Uses the existing DynamoDB cache to skip already-scanned functions
- No additional SQS queue needed - keeps costs minimal

Usage:
- Invoke manually to scan all functions in an account
- Schedule via EventBridge for periodic full scans (e.g., weekly)
- Pass account_ids list to scan across multiple accounts (centralized mode)

Environment Variables:
- SCANNER_FUNCTION_NAME: Name of the scanner Lambda to invoke
- CROSS_ACCOUNT_ROLE_NAME: Role name to assume in spoke accounts (optional)
- SCANNER_EXTERNAL_ID: External ID for cross-account role assumption
- EXCLUDE_PATTERNS: Comma-separated function name patterns to exclude
- INVOCATION_DELAY_MS: Delay between batches in ms (default: 100)
- MAX_WORKERS: Number of parallel invocation threads (default: 10)
- BATCH_SIZE: Functions per batch before pause (default: 100)
"""

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
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '10'))  # Parallel invocation threads
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '100'))  # Functions per batch before pause

# Validation patterns
ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')


def validate_account_id(account_id: str) -> bool:
    """Validate AWS account ID format."""
    return bool(ACCOUNT_ID_PATTERN.match(account_id))


def should_exclude(function_name: str, exclude_patterns: list) -> bool:
    """Check if function should be excluded from scanning."""
    for pattern in exclude_patterns:
        pattern = pattern.strip()
        if pattern and pattern in function_name:
            return True
    return False


def get_lambda_client_for_account(account_id: str) -> Optional[boto3.client]:
    """Get Lambda client for a specific account (cross-account)."""
    if not CROSS_ACCOUNT_ROLE_NAME:
        return None

    if not validate_account_id(account_id):
        logger.error(f"Invalid account ID format: {account_id}")
        return None

    try:
        role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName='BulkScanSession',
            DurationSeconds=3600,
            ExternalId=SCANNER_EXTERNAL_ID
        )

        return boto3.client(
            'lambda',
            aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
            aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
            aws_session_token=assumed_role['Credentials']['SessionToken']
        )
    except Exception as e:
        logger.error(f"Failed to assume role in account {account_id}: {e}")
        return None


def list_all_functions(client: boto3.client, exclude_patterns: list) -> List[Dict[str, Any]]:
    """List all Lambda functions using pagination with generator for memory efficiency."""
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
    """Invoke scanner Lambda asynchronously for a single function.

    Returns:
        Tuple of (success: bool, function_name: str)
    """
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
    """Invoke scanner for a batch of functions in parallel.

    Returns:
        Tuple of (invoked_count, failed_count)
    """
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


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Bulk scan handler.

    Event format:
    {
        "account_ids": ["123456789012", "234567890123"],  # Optional: cross-account
        "dry_run": false,  # Optional: just count, don't invoke scanner
        "exclude_patterns": ["test-", "dev-"]  # Optional: additional excludes
    }
    """
    logger.info(f"Bulk scan triggered with event: {json.dumps(event)}")

    # Validate scanner function is configured
    if not SCANNER_FUNCTION_NAME:
        return {
            'statusCode': 500,
            'body': {'error': 'SCANNER_FUNCTION_NAME not configured'}
        }

    # Parse event
    account_ids = event.get('account_ids', [])
    dry_run = event.get('dry_run', False)
    additional_excludes = event.get('exclude_patterns', [])

    # Create local exclude patterns list (avoid modifying global for thread safety)
    exclude_patterns = list(EXCLUDE_PATTERNS) + additional_excludes

    results = {
        'accounts_processed': 0,
        'accounts_failed': 0,
        'total_functions': 0,
        'invoked': 0,
        'failed': 0,
        'excluded': 0,
        'details': []
    }

    # Get current account ID
    current_account = sts_client.get_caller_identity()['Account']

    # If no account IDs specified, scan current account
    if not account_ids:
        account_ids = [current_account]

    for account_id in account_ids:
        account_id = str(account_id).strip()

        if not validate_account_id(account_id):
            logger.error(f"Invalid account ID: {account_id}")
            results['accounts_failed'] += 1
            continue

        logger.info(f"Processing account: {account_id}")

        try:
            # Get appropriate Lambda client for listing
            if account_id == current_account:
                list_client = lambda_client
            else:
                list_client = get_lambda_client_for_account(account_id)
                if not list_client:
                    results['accounts_failed'] += 1
                    results['details'].append({
                        'account': account_id,
                        'status': 'failed',
                        'error': 'Could not assume role'
                    })
                    continue

            # List all functions
            functions = list_all_functions(list_client, exclude_patterns)
            function_count = len(functions)
            results['total_functions'] += function_count

            logger.info(f"Found {function_count} functions in account {account_id}")

            if dry_run:
                results['details'].append({
                    'account': account_id,
                    'status': 'dry_run',
                    'functions': function_count
                })
            else:
                # Invoke scanner in parallel batches for performance at scale
                invoked = 0
                failed = 0

                # Process in batches to avoid overwhelming Lambda service
                for i in range(0, len(functions), BATCH_SIZE):
                    batch = functions[i:i + BATCH_SIZE]
                    batch_num = (i // BATCH_SIZE) + 1
                    total_batches = (len(functions) + BATCH_SIZE - 1) // BATCH_SIZE

                    logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} functions)")

                    batch_invoked, batch_failed = invoke_batch_parallel(batch, account_id)
                    invoked += batch_invoked
                    failed += batch_failed

                    # Pause between batches to avoid throttling
                    if i + BATCH_SIZE < len(functions) and INVOCATION_DELAY_MS > 0:
                        pause_seconds = (INVOCATION_DELAY_MS * BATCH_SIZE) / 1000.0
                        logger.info(f"Pausing {pause_seconds:.1f}s between batches")
                        time.sleep(pause_seconds)

                results['invoked'] += invoked
                results['failed'] += failed

                results['details'].append({
                    'account': account_id,
                    'status': 'success',
                    'functions': function_count,
                    'invoked': invoked,
                    'failed': failed
                })

            results['accounts_processed'] += 1

        except Exception as e:
            logger.error(f"Error processing account {account_id}: {e}")
            results['accounts_failed'] += 1
            results['details'].append({
                'account': account_id,
                'status': 'error',
                'error': str(e)
            })

    logger.info(f"Bulk scan complete: {json.dumps(results)}")

    return {
        'statusCode': 200,
        'body': results
    }

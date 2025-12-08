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

# Suppress noisy boto/botocore logs (e.g., "Found credentials in environment variables")
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

ENABLE_QUALYS_TAGGING = os.environ.get('ENABLE_QUALYS_TAGGING', 'false').lower() == 'true'

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

# API URLs for Asset Management Tagging API (qps)
QUALYS_API_MAP = {
    'US1': 'https://qualysapi.qg1.apps.qualys.com',
    'US2': 'https://qualysapi.qg2.apps.qualys.com',
    'US3': 'https://qualysapi.qg3.apps.qualys.com',
    'US4': 'https://qualysapi.qg4.apps.qualys.com',
    'GOV1': 'https://qualysapi.qg1.apps.qualys.com',
    'EU1': 'https://qualysapi.qg1.apps.qualys.eu',
    'EU2': 'https://qualysapi.qg2.apps.qualys.eu',
    'EU3': 'https://qualysapi.qg3.apps.qualys.it',
    'IN1': 'https://qualysapi.qg1.apps.qualys.in',
    'CA1': 'https://qualysapi.qg1.apps.qualys.ca',
    'AE1': 'https://qualysapi.qg1.apps.qualys.ae',
    'UK1': 'https://qualysapi.qg1.apps.qualys.co.uk',
    'AU1': 'https://qualysapi.qg1.apps.qualys.com.au',
    'KSA1': 'https://qualysapi.qg1.apps.qualysksa.com',
}

# Tag cache to avoid repeated API lookups
_qualys_tag_cache: Dict[str, str] = {}


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


def get_qualys_api_url(pod: str) -> str:
    """Get the Qualys API URL for Asset Management/Tagging API."""
    pod_upper = pod.upper()
    if pod_upper not in QUALYS_API_MAP:
        logger.warning(f"Unknown Qualys pod: {pod}, defaulting to US2")
        return QUALYS_API_MAP['US2']
    return QUALYS_API_MAP[pod_upper]


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


def qualys_api_request(
    gateway_url: str,
    endpoint: str,
    username: str,
    password: str,
    method: str = 'GET',
    data: Optional[Dict] = None,
    timeout: int = 30,
    max_retries: int = 3
) -> Tuple[int, Optional[Dict]]:
    """Make a request to the Qualys CS API with retry logic.

    Returns:
        Tuple of (status_code, response_json or None)
    """
    url = f"{gateway_url}{endpoint}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()

    headers = {
        'Authorization': f'Basic {auth}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    last_status = 0
    last_error = None

    for attempt in range(max_retries):
        try:
            if data:
                body = json.dumps(data).encode('utf-8')
            else:
                body = None

            request = urllib.request.Request(url, data=body, headers=headers, method=method)

            with urllib.request.urlopen(request, timeout=timeout) as response:
                status_code = response.status
                response_body = response.read().decode('utf-8')

                if response_body:
                    return status_code, json.loads(response_body)
                return status_code, None

        except urllib.error.HTTPError as e:
            last_status = e.code
            # Retry on 429 (rate limit), 500, 502, 503, 504
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Qualys API {e.code}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue

            # Try to parse error body - especially useful for 409 conflicts
            error_response = None
            try:
                error_body = e.read().decode('utf-8')
                if e.code != 409:  # Don't log 409 as error, it's expected
                    logger.error(f"Qualys API HTTP error: {e.code} - {e.reason}")
                    logger.error(f"Error body: {error_body[:500]}")
                else:
                    logger.info(f"Qualys API 409 conflict response: {error_body[:500]}")
                if error_body and error_body.strip().startswith('{'):
                    error_response = json.loads(error_body)
            except Exception as parse_err:
                logger.warning(f"Failed to parse error body: {parse_err}")

            return e.code, error_response

        except urllib.error.URLError as e:
            last_error = e
            # Retry on connection errors
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Qualys API connection error, retrying in {delay:.1f}s: {e.reason}")
                time.sleep(delay)
                continue
            logger.error(f"Qualys API URL error: {e.reason}")
            return 0, None

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Qualys API error, retrying in {delay:.1f}s: {e}")
                time.sleep(delay)
                continue
            logger.error(f"Qualys API request failed: {e}")
            return 0, None

    logger.error(f"Qualys API max retries exceeded: status={last_status}, error={last_error}")
    return last_status, None


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


def validate_qualys_tag_name(tag_name: str) -> bool:
    """Validate tag name for Qualys compatibility.

    Qualys tag names can be up to 1024 characters.
    """
    if not tag_name or not isinstance(tag_name, str):
        return False
    if len(tag_name) > 1024:
        return False
    # Allow alphanumeric, dash, underscore, dot, space, colon (for ARNs)
    if not re.match(r'^[a-zA-Z0-9\-_.: ]+$', tag_name):
        return False
    return True


def search_tag_by_name(
    gateway_url: str,
    username: str,
    password: str,
    tag_name: str,
    parent_tag_id: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """Search for a tag by name and optionally parent, return (id, uuid) tuple.

    Args:
        parent_tag_id: If provided, only return tag if it's a child of this parent (uses integer ID)

    Returns:
        Tuple of (tag_id, tag_uuid) or None if not found
        - tag_id: integer ID for use as parentTagId
        - tag_uuid: UUID for use in CS API tag assignment
    """
    # Check cache first (include parent in cache key for uniqueness)
    cache_key = f"tag:{parent_tag_id or 'root'}:{tag_name}"
    if cache_key in _qualys_tag_cache:
        return _qualys_tag_cache[cache_key]

    # Search for tag by name
    endpoint = "/qps/rest/2.0/search/am/tag"
    search_data = {
        "ServiceRequest": {
            "filters": {
                "Criteria": [
                    {
                        "field": "name",
                        "operator": "EQUALS",
                        "value": tag_name
                    }
                ]
            }
        }
    }

    status, response = qualys_api_request(gateway_url, endpoint, username, password, 'POST', search_data)

    if status == 200 and response:
        try:
            data = response.get('ServiceResponse', {}).get('data', [])
            if data:
                for item in data:
                    tag = item.get('Tag', {})
                    # Log all available tag fields for debugging
                    logger.info(f"AM API tag fields: {list(tag.keys())}")

                    # Get both id (integer) and uuid
                    tag_id = tag.get('id')  # Integer ID for parentTagId
                    tag_uuid = tag.get('tagUuid') or tag.get('uuid')  # UUID for CS API
                    tag_parent_id = tag.get('parentTagId')

                    if not tag_id:
                        continue

                    # If parent specified, only match if parent matches
                    if parent_tag_id:
                        if str(tag_parent_id) == str(parent_tag_id):
                            result = (str(tag_id), str(tag_uuid) if tag_uuid else str(tag_id))
                            _qualys_tag_cache[cache_key] = result
                            return result
                    else:
                        # No parent specified - return first match (for root-level tags)
                        if not tag_parent_id:
                            result = (str(tag_id), str(tag_uuid) if tag_uuid else str(tag_id))
                            _qualys_tag_cache[cache_key] = result
                            return result

        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Error parsing tag search response: {e}")

    return None


def create_tag(
    gateway_url: str,
    username: str,
    password: str,
    tag_name: str,
    parent_tag_id: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """Create a new tag in Qualys.

    Args:
        parent_tag_id: Integer ID of parent tag (not UUID)

    Returns:
        Tuple of (tag_id, tag_uuid) or None on failure
        - tag_id: integer ID for use as parentTagId
        - tag_uuid: UUID for use in CS API tag assignment
    """
    # Validate tag name
    if not validate_qualys_tag_name(tag_name):
        logger.error(f"Invalid tag name: {tag_name}")
        return None

    endpoint = "/qps/rest/2.0/create/am/tag"

    tag_data = {
        "ServiceRequest": {
            "data": {
                "Tag": {
                    "name": tag_name
                }
            }
        }
    }

    if parent_tag_id:
        # parentTagId must be an integer, not UUID
        tag_data["ServiceRequest"]["data"]["Tag"]["parentTagId"] = int(parent_tag_id)

    logger.info(f"Creating tag: name='{tag_name}', parent_id={parent_tag_id}")
    status, response = qualys_api_request(gateway_url, endpoint, username, password, 'POST', tag_data)

    if status in (200, 201) and response:
        try:
            tag = response.get('ServiceResponse', {}).get('data', [{}])[0].get('Tag', {})
            # Log all available tag fields for debugging
            logger.info(f"AM API create tag fields: {list(tag.keys())}")
            # Get both id and uuid
            tag_id = tag.get('id')
            tag_uuid = tag.get('tagUuid') or tag.get('uuid')
            if tag_id:
                cache_key = f"tag:{parent_tag_id or 'root'}:{tag_name}"
                result = (str(tag_id), str(tag_uuid) if tag_uuid else str(tag_id))
                _qualys_tag_cache[cache_key] = result
                logger.info(f"Created Qualys tag: {tag_name} -> id={tag_id}, uuid={tag_uuid}")
                return result
            else:
                logger.error(f"Tag created but no ID in response: {response}")
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Unexpected tag create response format: {e}")
    elif status == 401:
        logger.error("Qualys API authentication failed - check username/password")
    elif status == 403:
        logger.error("Qualys API access denied - check 'Create User Tag' permission")
    elif status == 409:
        # Tag already exists - this is not an error
        logger.info(f"Tag '{tag_name}' already exists (409 conflict)")

        # Try to extract existing tag ID from 409 response body
        if response:
            try:
                # Check various possible response structures for the existing tag
                existing_tag = None
                if 'ServiceResponse' in response:
                    data = response.get('ServiceResponse', {}).get('data', [])
                    if data and isinstance(data, list) and len(data) > 0:
                        existing_tag = data[0].get('Tag', {})
                elif 'Tag' in response:
                    existing_tag = response.get('Tag', {})

                if existing_tag:
                    tag_id = existing_tag.get('id')
                    tag_uuid = existing_tag.get('tagUuid') or existing_tag.get('uuid')
                    if tag_id:
                        cache_key = f"tag:{parent_tag_id or 'root'}:{tag_name}"
                        result = (str(tag_id), str(tag_uuid) if tag_uuid else str(tag_id))
                        _qualys_tag_cache[cache_key] = result
                        logger.info(f"Extracted existing tag from 409 response: {tag_name} -> id={tag_id}, uuid={tag_uuid}")
                        return result
            except Exception as e:
                logger.warning(f"Failed to extract tag ID from 409 response: {e}")

        # Fall back to searching for the tag with retries
        logger.info(f"Searching for existing tag: {tag_name}")
        for attempt in range(3):
            result = search_tag_by_name(gateway_url, username, password, tag_name, parent_tag_id)
            if result:
                logger.info(f"Found existing tag via search: {tag_name} -> id={result[0]}, uuid={result[1]}")
                return result
            if attempt < 2:
                time.sleep(1)  # Brief delay before retry

        logger.warning(f"Tag '{tag_name}' reported as existing but could not find it")
        return None
    else:
        logger.error(f"Failed to create tag '{tag_name}': status={status}, response={response}")

    return None


def get_or_create_tag(
    gateway_url: str,
    username: str,
    password: str,
    tag_name: str,
    parent_tag_id: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """Get existing tag or create new one.

    Args:
        parent_tag_id: Integer ID of parent tag (not UUID)

    Returns:
        Tuple of (tag_id, tag_uuid) or None on failure
        - tag_id: integer ID for use as parentTagId
        - tag_uuid: UUID for use in CS API tag assignment
    """
    logger.info(f"get_or_create_tag: name='{tag_name}', parent_id={parent_tag_id}")

    # Try to find existing tag with parent context
    result = search_tag_by_name(gateway_url, username, password, tag_name, parent_tag_id)
    if result:
        logger.info(f"Found existing tag: {tag_name} -> id={result[0]}, uuid={result[1]}")
        return result

    # Create new tag
    logger.info(f"Tag not found, creating: {tag_name}")
    return create_tag(gateway_url, username, password, tag_name, parent_tag_id)


def ensure_tag_hierarchy(
    gateway_url: str,
    username: str,
    password: str,
    function_arn: str
) -> Optional[str]:
    """Ensure the Lambda -> Region -> ARN tag hierarchy exists.

    Tag structure:
        Lambda (parent)
        └── <region> (child)
            └── <full_arn> (child)

    Returns:
        The ARN tag UUID (for use in CS API tag assignment) or None on failure
    """
    # Parse the ARN
    # arn:aws:lambda:<region>:<account_id>:function:<function_name>
    arn_parts = function_arn.split(':')
    if len(arn_parts) < 7:
        logger.error(f"Invalid function ARN format: {function_arn}")
        return None

    region = arn_parts[3]
    account_id = arn_parts[4]
    function_name = arn_parts[6]

    logger.info(f"Ensuring tag hierarchy for: region={region}, account={account_id}, function={function_name}")

    # Use full ARN as tag name (Qualys supports up to 1024 chars)
    arn_tag_name = function_arn

    # Step 1: Get or create parent "Lambda" tag (root level)
    logger.info("Step 1: Getting/creating root 'Lambda' tag...")
    lambda_tag_result = get_or_create_tag(gateway_url, username, password, "Lambda")
    if not lambda_tag_result:
        logger.error("Failed to get/create Lambda parent tag")
        return None
    lambda_tag_id, lambda_tag_uuid = lambda_tag_result
    logger.info(f"Lambda tag: id={lambda_tag_id}, uuid={lambda_tag_uuid}")

    # Step 2: Get or create region tag under Lambda (use integer ID as parent)
    logger.info(f"Step 2: Getting/creating region tag '{region}' under Lambda...")
    region_tag_result = get_or_create_tag(gateway_url, username, password, region, lambda_tag_id)
    if not region_tag_result:
        logger.error(f"Failed to get/create region tag: {region}")
        return None
    region_tag_id, region_tag_uuid = region_tag_result
    logger.info(f"Region tag: id={region_tag_id}, uuid={region_tag_uuid}")

    # Step 3: Get or create ARN tag under region (use integer ID as parent)
    logger.info(f"Step 3: Getting/creating ARN tag under region...")
    arn_tag_result = get_or_create_tag(gateway_url, username, password, arn_tag_name, region_tag_id)
    if not arn_tag_result:
        logger.error(f"Failed to get/create ARN tag: {arn_tag_name}")
        return None
    arn_tag_id, arn_tag_uuid = arn_tag_result
    logger.info(f"ARN tag: id={arn_tag_id}, uuid={arn_tag_uuid}")

    logger.info(f"Tag hierarchy complete: Lambda({lambda_tag_id}) -> {region}({region_tag_id}) -> ARN({arn_tag_id})")
    # Return the UUID for use in CS API tag assignment
    return arn_tag_uuid


def assign_tag_to_image(
    gateway_url: str,
    token: str,
    image_uuid: str,
    tag_uuid: str
) -> bool:
    """Assign a tag to an image in Qualys CS using Bearer token auth."""
    endpoint = "/csapi/v1.3/tag/assign"

    assign_data = {
        "entityUUID": image_uuid,
        "tagsToAdd": [
            {"tagUuid": tag_uuid, "isCascadeToContainer": False}
        ],
        "entityType": "IMAGE"
    }

    logger.info(f"Assigning tag: image_uuid={image_uuid}, tag_uuid={tag_uuid}")
    status, response = qualys_cs_api_request(gateway_url, endpoint, token, 'POST', assign_data)

    if status in (200, 201, 204):
        logger.info(f"Successfully assigned tag {tag_uuid} to image {image_uuid}")
        return True
    elif status == 400:
        # Check if tag is already assigned - treat as success
        error_msg = str(response) if response else ''
        if 'already' in error_msg.lower() or 'exist' in error_msg.lower():
            logger.info(f"Tag {tag_uuid} already assigned to image {image_uuid}")
            return True
        logger.error(f"Failed to assign tag to image: status={status}, response={response}")
        return False
    else:
        logger.error(f"Failed to assign tag to image: status={status}, response={response}")
        return False


def tag_qualys_image(qualys_creds: Dict[str, str], function_arn: str, image_sha: str) -> bool:
    """Tag a scanned image in Qualys CS with Lambda function information.

    This creates/uses the tag hierarchy: Lambda -> <region> -> <full_arn>

    Returns:
        True on success, False on failure
    """
    if not ENABLE_QUALYS_TAGGING:
        return True

    # Validate credentials
    token = qualys_creds.get('qualys_access_token', '').strip()
    username = qualys_creds.get('qualys_api_username', '').strip()
    password = qualys_creds.get('qualys_api_password', '').strip()
    pod = qualys_creds.get('qualys_pod', '').strip()

    if not pod:
        logger.error("Qualys tagging: 'qualys_pod' missing from credentials")
        return False

    if not token:
        logger.error("Qualys tagging: 'qualys_access_token' missing from credentials")
        return False

    if not username or not password:
        logger.error("Qualys tagging: username/password missing for Asset Management API")
        return False

    gateway_url = get_qualys_gateway_url(pod)
    api_url = get_qualys_api_url(pod)
    logger.info(f"Qualys tagging: pod={pod}")

    try:
        # Get image UUID from Qualys CS API with retry for timing delays
        image_data = None
        delays = [5, 10, 15]
        for attempt, delay in enumerate(delays):
            image_data = get_image_by_sha(gateway_url, token, image_sha)
            if image_data:
                break
            if attempt < len(delays) - 1:
                logger.info(f"Image not yet available, retrying in {delay}s...")
                time.sleep(delay)

        if not image_data:
            logger.warning(f"Image not found in Qualys after retries: {image_sha}")
            return False

        image_uuid = image_data.get('uuid')
        if not image_uuid:
            logger.error("Image response missing 'uuid' field")
            return False
        logger.info(f"Found image in Qualys: uuid={image_uuid}")

        # Ensure tag hierarchy exists using Asset Management API
        arn_tag_uuid = ensure_tag_hierarchy(api_url, username, password, function_arn)
        if not arn_tag_uuid:
            logger.error("Failed to create tag hierarchy")
            return False
        logger.info(f"Tag hierarchy ready: arn_tag_uuid={arn_tag_uuid}")

        # Assign tag to image using CS API
        return assign_tag_to_image(gateway_url, token, image_uuid, arn_tag_uuid)

    except Exception as e:
        logger.error(f"Error tagging Qualys image: {e}")
        return False


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

        # Partial success metric (SBOM uploaded but vuln report failed)
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

    # Exit codes that indicate partial success (SBOM uploaded but vuln report failed)
    # 40 = Vulnerability reporter failed (404 Not Found) - scan data was still uploaded
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
            'success': True,  # True for both full and partial success (SBOM uploaded)
            'partial': is_partial,  # True if vuln report failed but SBOM succeeded
            'exit_code': result.returncode,
            'results': scan_results,
            'stdout': result.stdout,
            'stderr': result.stderr
        }

    except subprocess.TimeoutExpired:
        logger.error(f"QScanner timed out after {SCAN_TIMEOUT} seconds")
        raise ScanException(f"Scan timeout after {SCAN_TIMEOUT} seconds")


def extract_repo_tags(scan_results: Dict[str, Any], scan_timestamp: str) -> Optional[str]:
    """Extract RepoTags from Metadata.TargetMetadata.ImageMetadata in scan results JSON"""
    try:
        if 'results' not in scan_results or not isinstance(scan_results['results'], dict):
            logger.warning("No results found in scan_results")
            return None

        results = scan_results['results']

        # Only check Metadata.TargetMetadata.ImageMetadata.RepoTags (QScanner output structure)
        if 'Metadata' not in results or not isinstance(results['Metadata'], dict):
            logger.warning("No Metadata found in scan results")
            return None

        metadata = results['Metadata']
        if 'TargetMetadata' not in metadata or not isinstance(metadata['TargetMetadata'], dict):
            logger.warning("No TargetMetadata found in Metadata")
            return None

        target_metadata = metadata['TargetMetadata']
        if 'ImageMetadata' not in target_metadata or not isinstance(target_metadata['ImageMetadata'], dict):
            logger.warning("No ImageMetadata found in TargetMetadata")
            return None

        repo_tags = target_metadata['ImageMetadata'].get('RepoTags', [])
        if not isinstance(repo_tags, list) or not repo_tags:
            logger.warning("No RepoTags found in ImageMetadata")
            return None

        repo_tag = repo_tags[0]
        logger.info(f"Found RepoTag in Metadata.TargetMetadata.ImageMetadata: {repo_tag}")

        if not validate_tag_value(repo_tag):
            logger.warning(f"Invalid RepoTag format or length: {repo_tag} (length: {len(repo_tag)})")
            return None

        return repo_tag

    except Exception as e:
        logger.error(f"Error extracting RepoTags: {e}")
        return None


def extract_image_sha(scan_results: Dict[str, Any]) -> Optional[str]:
    """Extract image SHA256 from Metadata.TargetMetadata.ImageMetadata in scan results JSON.

    Returns:
        Image SHA256 (with or without 'sha256:' prefix) or None if not found
    """
    try:
        if 'results' not in scan_results or not isinstance(scan_results['results'], dict):
            return None

        results = scan_results['results']

        if 'Metadata' not in results or not isinstance(results['Metadata'], dict):
            return None

        metadata = results['Metadata']
        if 'TargetMetadata' not in metadata or not isinstance(metadata['TargetMetadata'], dict):
            return None

        target_metadata = metadata['TargetMetadata']
        if 'ImageMetadata' not in target_metadata or not isinstance(target_metadata['ImageMetadata'], dict):
            return None

        image_metadata = target_metadata['ImageMetadata']

        image_id = image_metadata.get('ImageID')
        if image_id and isinstance(image_id, str) and len(image_id) >= 64:
            logger.info(f"Found image SHA from ImageID: {image_id[:16]}...")
            return image_id

        # Try RepoDigests as fallback
        repo_digests = image_metadata.get('RepoDigests', [])
        if repo_digests and isinstance(repo_digests, list) and len(repo_digests) > 0:
            # Format: registry/repo@sha256:abcd...
            digest = repo_digests[0]
            if '@sha256:' in digest:
                sha = digest.split('@sha256:')[1]
                logger.info(f"Found image SHA from RepoDigests: {sha[:16]}...")
                return sha

        return None

    except Exception as e:
        logger.error(f"Error extracting image SHA: {e}")
        return None


def tag_lambda_function(
    function_arn: str,
    repo_tag: Optional[str],
    scan_timestamp: str,
    scan_success: bool,
    scan_partial: bool = False,
    target_lambda_client: Optional[Any] = None
) -> None:
    """Tag Lambda function with scan results.

    Args:
        function_arn: ARN of the Lambda function to tag
        repo_tag: Optional repo tag from scan results
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

        # Only add QualysScanTag if repo_tag was found
        if repo_tag:
            # Extract just the timestamp portion (strip "lambdascan:" prefix if present)
            scan_tag = repo_tag.split(':', 1)[1] if ':' in repo_tag else repo_tag
            safe_scan_tag = scan_tag[:100] if len(scan_tag) > 100 else scan_tag
            tags['QualysScanTag'] = safe_scan_tag
            logger.info(f"Tagging Lambda with ScanTag: {safe_scan_tag}")
        else:
            logger.info("No RepoTag found, skipping QualysScanTag")

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

    repo_tag = extract_repo_tags(scan_results, timestamp)
    tag_lambda_function(
        lambda_details['function_arn'],
        repo_tag,
        timestamp,
        scan_results['success'],
        scan_results.get('partial', False),
        target_lambda_client
    )


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

        # Get Lambda client for target account (handles both standalone and hub-and-spoke)
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

        # Tag image in Qualys CS (if enabled and scan succeeded)
        if ENABLE_QUALYS_TAGGING and scan_results.get('success'):
            image_sha = extract_image_sha(scan_results)
            if image_sha:
                tag_qualys_image(qualys_creds, function_arn, image_sha)
            else:
                logger.warning("Could not extract image SHA for Qualys tagging")

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

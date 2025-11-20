# Security Review

## Critical Vulnerabilities Fixed

### 1. Secrets Exposure in CloudFormation ✅ FIXED
**Issue**: QualysAccessToken was passed as CloudFormation parameter (visible in event history)
**Fix**: Secret created separately via Makefile, only ARN passed to CloudFormation
```bash
# Secret creation happens before stack deployment
make create-secret
# Then CloudFormation only receives the ARN
```

### 2. Command Injection via Credentials ✅ FIXED
**Issue**: Credentials passed to subprocess without validation
**Fix**: Added strict validation:
- POD format: `^[A-Z0-9]+$`
- Token format: `^[a-zA-Z0-9_-]{20,200}$`
- ARN format: `^arn:aws:lambda:[a-z0-9-]+:\d{12}:function:[a-zA-Z0-9-_]{1,64}$`
- Function name: `^[a-zA-Z0-9-_]{1,64}$`

### 3. Information Disclosure in Logs ✅ FIXED
**Issue**: Credentials and sensitive data logged to CloudWatch
**Fix**:
- Sanitize all output before logging (redact tokens, passwords, secrets)
- Log only source/detail-type from events, not full event
- Generic error responses (only request_id returned)

### 4. Over-Permissive ECR Access ✅ FIXED
**Issue**: `Resource: '*'` for all ECR operations
**Fix**: Split into two policies:
- `ecr:GetAuthorizationToken` on `*` (required by AWS)
- All other ECR operations scoped to account repositories

### 5. Unnecessary S3 ACL Permission ✅ FIXED
**Issue**: Lambda could modify object ACLs
**Fix**: Removed `s3:PutObjectAcl`, only `s3:PutObject` remains

### 6. Missing Input Validation ✅ FIXED
**Issue**: EventBridge events processed without validation
**Fix**: All inputs validated before processing

## Remaining Security Considerations

### Medium Priority (Not Blockers)

1. **Cross-Account ExternalId**
   - Centralized deployment should use unique ExternalId per spoke
   - Currently uses static value
   - Low risk if deploying within trusted organization

2. **Encryption at Rest**
   - SNS topics: Consider adding KMS encryption
   - DynamoDB: Consider adding KMS encryption
   - CloudWatch Logs: Consider adding KMS encryption
   - Note: All use AWS-managed encryption by default

3. **CloudTrail Retention**
   - Currently 7 days
   - Consider extending to 90+ days for compliance
   - Or transition to Glacier for long-term retention

4. **Lambda Concurrency Limits**
   - No reserved concurrency set
   - Could exhaust account limits during mass deployments
   - Recommend setting ReservedConcurrentExecutions: 10

## Security Best Practices Implemented

✅ Secrets in AWS Secrets Manager (encrypted at rest)
✅ IAM least privilege policies
✅ Input validation on all external inputs
✅ Output sanitization for logging
✅ No secrets in environment variables passed to subprocess
✅ No secrets in CloudFormation parameters
✅ S3 buckets with encryption and versioning
✅ S3 buckets block all public access
✅ CloudTrail enabled for audit logging
✅ DynamoDB with TTL for automatic cleanup
✅ Lambda execution in AWS-managed VPC (default)

## Deployment Security Checklist

Before deploying to production:

- [ ] QScanner binary obtained from trusted Qualys source
- [ ] Verify binary checksum matches Qualys documentation
- [ ] Qualys access token generated with minimal required scopes
- [ ] Secret ARN permissions restricted to scanner Lambda only
- [ ] S3 results bucket has lifecycle policy
- [ ] SNS topic subscriptions validated
- [ ] CloudTrail enabled in all target accounts
- [ ] EventBridge rules tested with sample events
- [ ] Scanner Lambda timeout appropriate for largest images
- [ ] Lambda memory appropriate for QScanner requirements
- [ ] DynamoDB table billing mode appropriate (pay-per-request vs provisioned)
- [ ] Multi-region deployment uses separate stacks per region
- [ ] Cross-account roles tested with ExternalId

## Monitoring and Alerting

Recommended CloudWatch alarms:

1. Scanner Lambda errors > threshold
2. Scanner Lambda duration approaching timeout
3. DynamoDB throttling events
4. S3 bucket unauthorized access attempts
5. Secrets Manager access from unexpected principals
6. Scan failures > 10% of invocations

## Incident Response

If scanner Lambda is compromised:

1. Immediately rotate Qualys access token
2. Update secret in Secrets Manager
3. Review CloudWatch Logs for unauthorized scans
4. Review CloudTrail for IAM/ECR access patterns
5. Check S3 bucket for unauthorized access
6. Disable EventBridge rules temporarily
7. Update Lambda function code from trusted source
8. Re-enable after verification

## Compliance Notes

This solution supports compliance requirements:

- **SOC 2**: Automated security scanning, audit logging via CloudTrail
- **PCI-DSS**: Vulnerability scanning requirement (ASV)
- **HIPAA**: Encryption at rest/transit, audit logs
- **FedRAMP**: GovCloud POD support, CloudTrail integration
- **GDPR**: No PII processed by scanner

## Binary Loading - Security

QScanner binary (>100MB) MUST use Docker deployment:

1. Binary packaged into Docker image at build time
2. Image pushed to private ECR repository
3. ECR repository encrypted at rest
4. Lambda references ECR image URI
5. No runtime download or installation
6. Binary integrity verified by Docker image digest

**DO NOT**:
- Download binary at Lambda runtime
- Store binary in S3 and download
- Use unsigned/unverified binaries
- Share QScanner tokens across environments

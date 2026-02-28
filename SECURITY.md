# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

Only the latest minor release receives security updates. Older versions are not patched.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

To report a vulnerability:

1. Email **security@vektralabs.com** with:
   - Description of the vulnerability
   - Steps to reproduce
   - Affected component(s) and version(s)
   - Impact assessment (if known)

2. You will receive an acknowledgment within **48 hours**.

3. We will investigate and provide a timeline for a fix within **7 business days**.

4. Once a fix is released, we will credit you in the advisory (unless you prefer anonymity).

## Disclosure policy

- We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure).
- We ask reporters to give us a reasonable window (typically 90 days) before public disclosure.
- We will publish a GitHub Security Advisory for confirmed vulnerabilities.

## Security practices

- API keys are hashed with argon2id before storage
- Conversations are encrypted at rest via pgcrypto
- TLS termination is expected at the reverse proxy layer
- Soft delete for compliance with retention policies
- Audit logging for all state-changing operations
- Module boundary enforcement prevents internal cross-component access

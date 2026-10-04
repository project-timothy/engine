# Security policy

This engine keeps books for small organizations, so a security problem here can
put someone's money or records at risk. Thank you for reporting one privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Only the maintainers see
the report. Keep it out of public issues and pull requests.

Tell us what you found and how to reproduce it. A maintainer acknowledges every
report within a week, then keeps you informed until the fix ships.

## What is in scope

- The engine and auditor code in this repository.
- The container image and the scheduled scripts.
- The documentation, where it tells an operator to do something unsafe.

A deployment's own configuration, credentials, and host are the operator's
responsibility. If you find one exposed, tell its operator directly.

## What never belongs in this repository

No credential, customer, vendor, amount, or host detail from any real
deployment. If you see one, report it the same private way.

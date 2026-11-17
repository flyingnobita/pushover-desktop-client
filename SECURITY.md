# Security policy

## Reporting a vulnerability

Report vulnerabilities through GitHub private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. That report is private between you and the
maintainer. Do not open a public issue for a vulnerability.

Expect an acknowledgement within a few days. This is a personal project maintained in spare time,
so a fix may take longer than the acknowledgement.

## Supported versions

Only the latest commit on `main` is supported. This project is deployed from source, so a fix
lands on `main` and you pick it up by rebuilding.

## Out of scope

- The Pushover service itself. Report problems with Pushover to Pushover.
- Anything exposed by deliberately setting `AUTH_DISABLED=true`. That setting turns off the login
  screen, so anyone who can reach the port can read and delete the stored history. It is a
  documented, intentional trade-off for a trusted network, not a vulnerability.

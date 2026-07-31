# Security Policy

## Supported versions

Security updates are provided for the latest release on the `main` branch.
Older releases may be asked to update before receiving a fix.

| Version | Supported |
| --- | --- |
| Latest release / `main` | Yes |
| Older releases | No |

## Reporting a vulnerability

Please do not report security vulnerabilities through a public issue,
discussion, pull request, screenshot or Discord message.

Use the repository's **Security** page and select **Report a vulnerability**.
This sends the details privately through GitHub's private vulnerability
reporting system.

If that button is unavailable, open a public issue asking the maintainer for a
private security contact, but include no vulnerability details in that issue.

Please include:

- The affected version or commit
- A description of the vulnerability and its potential impact
- Reproduction steps or a minimal proof of concept
- Any suggested mitigation
- Whether the vulnerability is already publicly known

The maintainer will aim to acknowledge a complete report within seven days.
Please allow time to investigate and prepare a fix before publishing details.

## Sensitive information

If a Discord token, SteamGridDB key, IGDB secret or other credential is
exposed, revoke and rotate it immediately. Removing the value from the latest
commit is not enough because Git history may retain it.

Do not attach real `.env` files, private `config.json` files, databases,
backups or server logs containing personal information to public reports.


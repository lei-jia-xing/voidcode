# Security Policy

We take the security of VoidCode seriously. If you discover a potential vulnerability, please report it privately rather than opening a public issue.

## Supported versions

VoidCode is shipping its first productionized runtime release. At this stage, security fixes are only supported for the latest code on the `master` branch.

| Version | Supported |
| :--- | :--- |
| `master` | ✅ |
| historical revisions | ❌ |

## Reporting a vulnerability

**Do not report security-sensitive issues through public GitHub issues.**

Please use GitHub's private vulnerability reporting flow:

[Report a vulnerability](https://github.com/lei-jia-xing/voidcode/security/policy)

When possible, include:

- the affected component (for example: runtime, CLI behavior, or a specific tool)
- a description of the issue and its impact
- reproduction steps, sample input, configuration, or proof-of-concept details
- environment details such as OS, Python version, Bun version, and the affected commit hash

## What to expect from us

After receiving a report, we will:

1. acknowledge receipt and perform an initial review
2. assess the severity and priority
3. work on a private fix and validate it
4. release the fix according to the project's maintenance cadence and publish a security advisory when appropriate

Please keep vulnerability details private until a fix has been prepared and disclosed responsibly.

## Acknowledgements

We do not currently run a formal bug bounty program, but we appreciate responsible disclosure and the help of security researchers and contributors.

---

For non-security contributions, see [CONTRIBUTING.md](./CONTRIBUTING.md).

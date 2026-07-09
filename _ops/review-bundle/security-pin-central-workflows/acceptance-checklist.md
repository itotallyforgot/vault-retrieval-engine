# Acceptance checklist — security-pin-central-workflows

- [✓] Reusable CI and security workflows use the immutable central merge commit.
- [✓] The security caller grants only the additional pull-request metadata permission needed by gitleaks.
- [✓] Dependabot cooldowns cover Python and GitHub Actions.
- [✓] Repository zizmor policy requires hashes for every workflow use.
- [✓] Local zizmor validation reported no findings.
- [⚠] Foreign-model adversarial review — skipped by explicit user direction.

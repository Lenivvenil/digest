# Security reporting and limits

Do not publish credentials, private configuration, recipient identifiers, runtime artifacts or exploitable vulnerability details in an issue or pull request.

As of 2026-10-13, this repository has no established private vulnerability-reporting channel. A public issue may request a private reporting route using only non-sensitive coordination details. Wait for a verified private route before sharing sensitive evidence; do not assume a public issue or an unverified contact is confidential. No response-time or disclosure-timeline commitment is made here.

For a report that can safely be discussed publicly, provide a minimal synthetic reproduction and the immutable engine commit and Python version. These identify the observed behavior; they do not establish a security-support window for every historical runtime pin.

[Contribution guidance](https://github.com/Lenivvenil/digest/blob/main/.github/CONTRIBUTING.md) describes the checks actually run. Passing CI does not establish a vulnerability-free system. The [dated security evidence in #148](https://github.com/Lenivvenil/digest/issues/148) distinguishes dependency advisories, source review and repository-alert configuration. Follow [upgrade and retained-state guidance](https://github.com/Lenivvenil/digest/blob/main/docs/OPERATIONS.md#upgrades-and-retained-state); do not erase runtime state or resend an uncertain delivery to test a security report.

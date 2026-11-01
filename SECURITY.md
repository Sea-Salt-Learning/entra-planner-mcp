# Security policy

## Reporting a vulnerability
Please report vulnerabilities **privately** through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue.

We aim to acknowledge reports within 5 working days and to ship a fix for confirmed high or critical issues
within 14 days. Please don't include real tokens, tenant identifiers or personal data in a report.

## Supported versions
Only the latest image published from `main` is supported. Images are rebuilt weekly and on dependency
updates; deploy by digest and roll forward regularly.

## Design summary
- Stateless: no database, no stored tokens, no secrets. The On-Behalf-Of exchange uses a managed identity
  federated credential, so nothing secret is configured.
- Microsoft Entra ID is the only authorization server. Every request to `/mcp` needs a v2.0 Entra access
  token for this API (signature, issuer, audience, tenant, scope, expiry checked); app-only tokens are refused.
- Acts only as the signed-in user, with delegated `Tasks.ReadWrite`. There is no delete tool, no generic Graph
  passthrough, and write tools are off unless enabled in `ALLOWED_TOOLS`.
- Logs never contain tokens or task content.
- Images: distroless/Chainguard base, non-root, dependencies locked with hashes, Trivy-gated, signed with
  cosign (keyless), with a CycloneDX SBOM attestation and SLSA build provenance.

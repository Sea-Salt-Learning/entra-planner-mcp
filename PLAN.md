# Entra Planner MCP - build plan

A small, stateless remote MCP server that lets Claude (claude.ai, Desktop, mobile, Cowork, and Claude Code via
organisation connectors) read and update **Microsoft Planner tasks in group plans**, acting as the signed-in user.
Microsoft Entra ID is the only authorization server. The server stores nothing and holds no secrets.

This repository is generic and public. It must never contain anything that identifies the organisation that
deploys it: no tenant IDs, app IDs, hostnames, domains, user names, emails, plan or group names or IDs. Every
deployment-specific value comes from environment variables.

## Architecture
```
MCP client (Claude) --401 + resource_metadata--> this server (/.well-known/oauth-protected-resource)
        |                                              |
        +-- user signs in to Entra (PKCE) --> token for the API app (aud = API app ID, scp = Planner.Use)
                                                       |
this server: validate token -> On-Behalf-Of (client assertion = managed identity federated credential)
                                                       |
                                            Microsoft Graph Planner API as the user
```

## Configuration (environment only)
| Variable | Meaning |
|---|---|
| `ENTRA_TENANT_ID` | Tenant GUID. Tokens from any other tenant are rejected |
| `ENTRA_API_CLIENT_ID` | API app registration ID (expected `aud`) |
| `ENTRA_REQUIRED_SCOPE` | Delegated scope name, default `Planner.Use` |
| `PUBLIC_BASE_URL` | Canonical public URL of the MCP endpoint, e.g. `https://example.azurecontainerapps.io/mcp` (no trailing slash) |
| `ALLOWED_TOOLS` | Comma list; default the read-only tools only |
| `OBO_CREDENTIAL` | `managed_identity` (default; client assertion from the platform's managed identity, audience `api://AzureADTokenExchange`) or `local_dev_secret` (local only, refused when `ENV=production`) |
| `MANAGED_IDENTITY_CLIENT_ID` | Optional, for user-assigned identity |
| `RATE_LIMIT_PER_MIN` | Per-user calls per minute, default 60 |
| `LOG_LEVEL` | Default `WARNING`; never logs token or task content |

## Auth requirements (Claude connector rules)
- Unauthenticated `POST /mcp` -> `401` with `WWW-Authenticate: Bearer resource_metadata="<PUBLIC_BASE_URL origin>/.well-known/oauth-protected-resource/mcp", scope="api://<API app>/Planner.Use offline_access"`.
- Protected resource metadata (RFC 9728): `resource` = `PUBLIC_BASE_URL` exactly; `authorization_servers` =
  `["https://login.microsoftonline.com/<tenant>/v2.0"]` (single entry); `scopes_supported`.
- Serve it at both `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-protected-resource`.
- Token validation: signature via the tenant's JWKS (cached, rotated), `iss` = `https://login.microsoftonline.com/<tenant>/v2.0`,
  `aud` = API app ID, `tid` = tenant, `scp` contains the required scope, `exp`/`nbf` with small skew, `ver` = `2.0`.
  Reject app-only tokens (no `scp`). Use a maintained JWT library, not hand-rolled crypto.
- No HTTP redirects on the MCP path. HTTPS terminated by the platform.

## Tools (no delete tool exists)
| Tool | Graph | Kind |
|---|---|---|
| `planner_list_plans` | `GET /me/planner/plans` | read |
| `planner_get_plan` | plan, buckets, tasks with filters (bucket, assignee, due before/after, open/completed) | read |
| `planner_get_task` | task + details (description, checklist, references) | read |
| `planner_my_tasks` | `GET /me/planner/tasks` | read |
| `planner_create_task` | `POST /planner/tasks` then details | write |
| `planner_update_task` | `PATCH` task/details with `If-Match` ETag (title, due, bucket, percentComplete, priority, description, checklist items) | write |
| `planner_complete_task` | `PATCH percentComplete=100` with ETag | write |
| `planner_assign_task` | add/remove assignees (user object IDs; resolve by email via `GET /users/{upn}` only if `User.ReadBasic.All` is granted, else IDs only) | write |

Rules: validate all IDs/lengths; return compact JSON; map Graph errors to clear MCP errors (403 -> "you don't
have access to this plan", 412 -> "changed by someone else, re-read and retry"); never pass arbitrary Graph paths.
Graph delegated scopes needed: `Tasks.ReadWrite`, `User.Read`, `offline_access` (and optionally
`User.ReadBasic.All` for assignee lookup - deployment choice).

## Image
- Python 3.12, official `mcp` SDK, streamable HTTP. `msal` for OBO, `pyjwt[crypto]` (or `joserfc`) for validation, `httpx`.
- Multi-stage build, final stage distroless/Chainguard Python, non-root, no shell, read-only FS friendly
  (no writes outside `/tmp` if any), `EXPOSE 8000`, health endpoints `/health/live`, `/health/ready` (ready =
  config valid + JWKS fetched).
- Dependencies locked with hashes.

## CI (GitHub Actions, public repo; no cloud credentials)
- On PR and push: ruff, mypy, pytest (with a fake JWKS + fake Graph), gitleaks, identifier deny-list check
  (deny-list supplied privately via a repository **secret** `DENYLIST_REGEX`, never committed), Trivy fs scan.
- On push to `main`, weekly schedule, and Dependabot merges: build, Trivy image scan **fail on HIGH/CRITICAL with
  a fix**, CycloneDX SBOM, push to `ghcr.io/<owner>/entra-planner-mcp` (tags: `sha-<sha>`, `latest`), cosign
  keyless sign + attest SBOM, SLSA provenance (`actions/attest-build-provenance`).
- All actions pinned by commit SHA; `permissions:` least privilege per job; Dependabot for pip, Docker, Actions.
- `SECURITY.md` (private vulnerability reporting via GitHub), `LICENSE` (MIT), `README.md` with deployment guide
  for any tenant (Entra app setup, Container Apps example, Claude custom connector settings).

## Definition of done (public side)
1. Tests pass, including: 401 shape, metadata shape, every token-rejection case, tool allow-list, ETag conflict,
   no-delete guarantee, no logging of token/content.
2. Image published, signed, SBOM + provenance attested, Trivy clean.
3. Deny-list and gitleaks clean across full git history.

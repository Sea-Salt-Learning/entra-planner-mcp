# Entra Planner MCP

A small, stateless remote [MCP](https://modelcontextprotocol.io) server that lets Claude (claude.ai, Desktop,
mobile, Cowork, and Claude Code through organisation connectors) read and update **Microsoft Planner tasks in
group plans**, acting as the signed-in user.

- **Microsoft Entra ID is the only authorization server.** The server runs no OAuth endpoints of its own; it
  publishes [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) protected-resource metadata that points clients
  at your tenant.
- **Stateless, no secrets.** Each call validates the Entra access token and does an On-Behalf-Of (OBO) exchange
  for a Graph token. The OBO client credential is a **managed identity federated credential**, so there is no
  client secret anywhere.
- **Narrow by design.** Eight Planner tools, no delete tool, no generic Graph passthrough. Write tools are off
  unless you enable them.

```
MCP client (Claude) --401 + resource_metadata--> this server (/.well-known/oauth-protected-resource)
        |                                              |
        +-- user signs in to Entra (PKCE) --> token for the API app (aud = API app ID, scp = Planner.Use)
                                                       |
this server: validate token -> On-Behalf-Of (client assertion = managed identity federated credential)
                                                       |
                                            Microsoft Graph Planner API as the user
```

## Tools

| Tool | What it does | Kind |
|---|---|---|
| `planner_list_plans` | Plans in the groups you belong to (`GET /me/planner/plans`) | read |
| `planner_get_plan` | Buckets and tasks, filtered by bucket, assignee, due before/after, open/completed | read |
| `planner_get_task` | Task plus description, checklist and links, with ETags | read |
| `planner_my_tasks` | Tasks assigned to you (`GET /me/planner/tasks`) | read |
| `planner_create_task` | Create a task (bucket, due date, priority, notes, checklist, assignees) | write |
| `planner_update_task` | Update title, due date, bucket, progress, priority, notes, checklist (add/check/uncheck) | write |
| `planner_complete_task` | Set progress to 100% | write |
| `planner_assign_task` | Add or remove assignees (user object IDs or `me`) | write |

Writes use Planner ETags (`If-Match`). Pass the `etag` from `planner_get_task` for true optimistic concurrency;
otherwise the server reads the current ETag first. A conflict returns "changed by someone else: re-read it and
retry". Premium (Project) plans are not in Graph and are out of scope.

## Configuration (environment only)

| Variable | Required | Meaning |
|---|---|---|
| `ENTRA_TENANT_ID` | yes | Tenant GUID. Tokens from any other tenant are rejected |
| `ENTRA_API_CLIENT_ID` | yes | API app registration (client) ID; the expected `aud` |
| `PUBLIC_BASE_URL` | yes | Canonical public URL of the MCP endpoint, e.g. `https://<app>.<env>.<region>.azurecontainerapps.io/mcp` (no trailing slash) |
| `ENTRA_REQUIRED_SCOPE` | | Delegated scope name, default `Planner.Use` |
| `ENTRA_REQUIRED_ROLE` | | If set (e.g. `Planner.User`), tokens must carry this app role in `roles`; otherwise **403** with no error code (authenticated but not permitted; no scope step-up can fix it). **Recommended** - see step 2.7 |
| `ENTRA_ALLOWED_AUDIENCES` | | Extra accepted audiences (comma list); default the API client ID only |
| `ENTRA_ALLOWED_CLIENT_IDS` | | If set, only tokens issued to these client apps (`azp`) are accepted. Recommended |
| `ALLOWED_TOOLS` | | Comma list of tools to expose; default the four read-only tools |
| `OBO_CREDENTIAL` | | `managed_identity` (default) or `local_dev_secret` (refused when `ENV=production`) |
| `MANAGED_IDENTITY_CLIENT_ID` | | Client ID of a user-assigned identity; omit for system-assigned |
| `LOCAL_DEV_CLIENT_SECRET` | | Only with `OBO_CREDENTIAL=local_dev_secret` |
| `ENABLE_USER_LOOKUP` | | `true` lets assignees be given by email (needs Graph `User.ReadBasic.All`); default IDs only |
| `RATE_LIMIT_PER_MIN` | | Per-user tool calls per minute, default 60 |
| `LOG_LEVEL` | | Default `WARNING`. `DEBUG` is refused in production. Tokens and task content are never logged |
| `ALLOWED_ORIGINS` | | Accepted browser `Origin` values, default `https://claude.ai,https://claude.com` (requests without `Origin` are accepted) |
| `ENV` | | `production` (default) or `development` |

Start-up is optimised for scale-to-zero hosting: protected-resource metadata, health and the unauthenticated `401`
are answered within about a second of the process starting, while the MCP SDK loads in the background (only an
authenticated MCP call waits for it, up to 30 s, then gets `503` + `Retry-After`).

Endpoints: `POST /mcp` (streamable HTTP, stateless, JSON responses), `GET /.well-known/oauth-protected-resource`
and `/.well-known/oauth-protected-resource/mcp`, `GET /health/live`, `GET /health/ready` (ready = configuration
valid and the tenant's signing keys fetched). Requests whose `Host` isn't the `PUBLIC_BASE_URL` host get `421`.

## Deploying to your tenant

You need an Entra administrator (to create app registrations and grant admin consent) and an Azure
subscription. Replace every `<...>` placeholder.

### 1. Hosting first, so you know the URL
The MCP URL must be registered on the API app, so create the hosting first. Azure Container Apps (consumption,
scale to zero) fits within the monthly free grant for a handful of users:

```bash
RG=<resource-group>; LOC=<region>; ENVNAME=<env-name>; APP=<app-name>
az group create -n $RG -l $LOC
az containerapp env create -n $ENVNAME -g $RG -l $LOC --logs-destination none
FQDN="$APP.$(az containerapp env show -n $ENVNAME -g $RG --query properties.defaultDomain -o tsv)"
echo "MCP URL: https://$FQDN/mcp"
```

### 2. API app registration (the server)
1. Register an app (single tenant), e.g. "Planner MCP API". **No client secret.**
2. Manifest: set `api.requestedAccessTokenVersion` to `2` **before** adding the URL below. (Needed when the URL
   is on a domain your tenant hasn't verified, such as `*.azurecontainerapps.io`.)
3. Expose an API: Application ID URIs `api://<api-app-id>` **and the exact MCP URL** (`https://<fqdn>/mcp`).
   Claude sends the MCP URL as the OAuth `resource`; without it Entra fails with `AADSTS9010010`.
4. Add a delegated scope `Planner.Use`, "Admins only" consent.
5. API permissions, Microsoft Graph delegated: `Tasks.ReadWrite`, `User.Read`, `offline_access` (and
   `User.ReadBasic.All` only if you set `ENABLE_USER_LOOKUP=true`). Grant admin consent.
6. Enterprise application: **Assignment required = Yes**; assign the users who may use the connector.
7. **Also require an app role (recommended).** "Assignment required" is not enforced for every sign-in (for example
   some administrator accounts have been seen to obtain tokens for an app they are not assigned to), so don't rely on
   it alone. Add an app role (e.g. value `Planner.User`, allowed member types *Users/Groups*), assign each user **to
   that role** on the API's enterprise application, and set `ENTRA_REQUIRED_ROLE=Planner.User`. Only users holding
   the role get it in the token's `roles` claim; everyone else is refused by the server with 403.

### 3. Client app registration (what Claude signs in with)
1. Register a second app, e.g. "Planner MCP for Claude".
2. Platform **Mobile and desktop applications** (public client, PKCE, no secret) with redirect URIs
   `https://claude.ai/api/mcp/auth_callback`, `http://localhost/callback`, `http://127.0.0.1/callback`
   (Entra ignores the port for loopback addresses).
3. API permissions: your API's `Planner.Use`, plus Microsoft Graph `openid` and `offline_access`; grant admin
   consent (with user consent disabled, users can't consent themselves).
4. Enterprise application: Assignment required = Yes; assign the same users.
5. Optional hardening: set `ENTRA_ALLOWED_CLIENT_IDS=<client-app-id>` on the server.

If your Claude organisation can't use a public client, add a client secret to this client app (not the API app)
and enter it in Claude's connector settings only.

### 4. Deploy the container
Always deploy **by digest**, after verifying the signature:

```bash
IMAGE=ghcr.io/<owner>/entra-planner-mcp
DIGEST=$(docker buildx imagetools inspect $IMAGE:latest --format '{{json .Manifest}}' | jq -r .digest)
cosign verify $IMAGE@$DIGEST \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/<owner>/entra-planner-mcp/.github/workflows/release.yml@refs/heads/main

az containerapp create -n $APP -g $RG --environment $ENVNAME \
  --image $IMAGE@$DIGEST --target-port 8000 --ingress external \
  --cpu 0.25 --memory 0.5Gi --min-replicas 0 --max-replicas 2 \
  --system-assigned \
  --env-vars ENTRA_TENANT_ID=<tenant-id> ENTRA_API_CLIENT_ID=<api-app-id> \
             ENTRA_ALLOWED_CLIENT_IDS=<client-app-id> ENTRA_REQUIRED_ROLE=Planner.User PUBLIC_BASE_URL=https://$FQDN/mcp \
             ALLOWED_TOOLS=planner_list_plans,planner_get_plan,planner_get_task,planner_my_tasks
az containerapp ingress update -n $APP -g $RG --allow-insecure false
```

Add HTTP probes on port 8000: startup `/health/live` (period 1 s, failure threshold 30), liveness `/health/live`, readiness `/health/ready` (period 3 s, timeout 3 s). Short startup/readiness periods matter with scale to zero: traffic waits for the first passing readiness probe. The managed identity
needs **no Azure role assignments**.

Optionally restrict ingress to your MCP client's egress range (for Claude, see Anthropic's published IP
addresses):
`az containerapp ingress access-restriction set -n $APP -g $RG --rule-name mcp-client --ip-address <cidr> --action Allow`.

### 5. Federated credential: let the managed identity act as the API app
```bash
PRINCIPAL=$(az containerapp show -n $APP -g $RG --query identity.principalId -o tsv)
az ad app federated-credential create --id <api-app-id> --parameters "{
  \"name\": \"container-app-mi\",
  \"issuer\": \"https://login.microsoftonline.com/<tenant-id>/v2.0\",
  \"subject\": \"$PRINCIPAL\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

### 6. Check it
```bash
curl -si -X POST https://$FQDN/mcp | grep -i www-authenticate   # 401, Bearer resource_metadata=...
curl -s https://$FQDN/.well-known/oauth-protected-resource/mcp  # resource = your MCP URL
```
(If you restricted ingress by IP, run these from an allowed address.)

### 7. Add the connector in Claude
Organization settings > Connectors > Add > Custom: the MCP URL, then the advanced/OAuth settings: **client ID**
= the client app's ID, client secret blank (public client). Set per-tool permissions (for example reads
"Always allow", writes "Ask").

## Development

```bash
uv sync --locked
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Tests use a fake JWKS and a fake Graph; nothing touches the network. For a local run against a real tenant, use a
**separate dev app registration** with a secret and `ENV=development OBO_CREDENTIAL=local_dev_secret` (see
`.env.example`); production refuses that mode.

## Supply chain
- Base image: Chainguard Python (distroless runtime: no shell, no package manager), non-root (UID 65532),
  read-only root filesystem friendly. Dependencies locked with hashes in `uv.lock`.
- CI on every PR and push: ruff, mypy, pytest, gitleaks (full history), an identifier deny-list (supplied as a
  repository secret, never committed), Trivy filesystem scan.
- Release on `main`, weekly, and after Dependabot merges: build, Trivy image scan (fails on any HIGH/CRITICAL with
  a fix), smoke test, CycloneDX SBOM, push to GHCR, cosign keyless signature + SBOM attestation, SLSA build
  provenance. Actions pinned by commit SHA with least-privilege permissions.

Maintainers: `main` only moves by fast-forward to a commit whose checks passed (`git push origin <sha>:main`).
Don't use GitHub's merge or squash buttons for human PRs: GitHub-made commits carry the merging account's
primary e-mail unless that account keeps its e-mail private.

See [SECURITY.md](SECURITY.md) to report a vulnerability. Licence: [MIT](LICENSE).

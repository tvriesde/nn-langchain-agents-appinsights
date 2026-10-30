# The Agent's Table

A small, public recipe chat demonstrating **LangChain + Azure Container Apps +
Application Insights**. A Chef creates a recipe, a Wine Agent selects a pairing,
and a Review Agent presents a coherent menu. The agents execute sequentially.

React/TypeScript is built into a single Python/FastAPI container. Azure Cosmos DB
for NoSQL stores anonymous conversations. The Microsoft OpenTelemetry distro
exports native GenAI agent/model spans to workspace-based Application Insights.
There is no LangSmith dependency or requirement for a Foundry project.

> **Demo, not a production service.** Anyone can generate billable model requests.
> Requests and agent outputs are captured in telemetry by default. Do not enter
> sensitive information. Basic throttling is not production abuse protection.

## What is deployed

Everything belongs to **one resource group**, with `SecurityControl: Ignore`
on the group and taggable resources. This tag does not bypass Azure policy.

| Resource | Configuration |
|---|---|
| Container App + consumption environment | Switzerland North; 0.5 CPU / 1 GiB; one replica; HTTPS |
| Container Registry | Switzerland North; Basic; no admin credentials |
| Cosmos DB NoSQL | Switzerland North; serverless; Session consistency; no automatic expiry |
| Application Insights + Log Analytics | Switzerland North; workspace retention 30 days |
| Azure OpenAI | `gpt-6-luna`, GlobalStandard; Switzerland North preferred, Sweden Central fallback |
| User-assigned managed identity | OpenAI User, Cosmos data contributor and AcrPull access |

Application Insights also creates a globally scoped Failure Anomalies alert in
the same resource group. This platform companion is not a regional workload;
the deployment script applies the required tag to it as well.

Model version defaults to `2026-09-22`. The deployment script checks the exact
model/version, supported capacity and subscription quota before creating resources.
Optional null capacity bounds/increments in Azure's model catalogue are treated
as unspecified constraints, not as zero.
It never substitutes another model or selects an unapproved region.
GlobalStandard **does not** restrict inference processing to the resource's region.

The app and SDKs use Entra credentials, not OpenAI or Cosmos keys. An optional
developer principal can receive data-plane access for local development.

## Deploy it yourself

Implementation does not automatically deploy anything. Requirements:

- PowerShell 7+, Azure CLI with Bicep available, and `az login`.
- Select the desired subscription yourself. The script reads, displays and uses
  the **current Azure CLI subscription**, without switching it.
- Resource provisioning permissions and `Microsoft.Authorization/roleAssignments/write`
  at the target scope. Owner, or Contributor plus Role Based Access Control
  Administrator, is a typical combination; Contributor alone is insufficient.
- Exact model access/quota in one approved region; quota availability does not
  guarantee physical capacity at deployment time.
- Access to ACR Tasks: the script builds remotely for Linux/amd64. Local Docker
  is not required. Subscription restrictions on ACR Tasks must be resolved first.
- The remote builder must reach the configured Python mirror, npm, MCR/Docker
  image registries and GHCR. Authentication is not baked into the image.

Register resource providers if they are not already registered:

```powershell
az login
az account show --query "{name:name,id:id}" -o table

@(
  'Microsoft.App', 'Microsoft.ContainerRegistry', 'Microsoft.CognitiveServices',
  'Microsoft.DocumentDB', 'Microsoft.Insights', 'Microsoft.OperationalInsights',
  'Microsoft.ManagedIdentity'
) | ForEach-Object { az provider register --namespace $_ --wait }

pwsh -File .\scripts\deploy.ps1
```

Optional arguments:

```powershell
# Explicitly grant this developer principal OpenAI and Cosmos data access.
$developer = az ad signed-in-user show --query id -o tsv
pwsh -File .\scripts\deploy.ps1 `
  -ResourceGroup rg-recipe-demo `
  -NamePrefix recipedemo `
  -ModelCapacity 10 `
  -DeveloperPrincipalId $developer

# Do not capture prompt/output text in Application Insights.
pwsh -File .\scripts\deploy.ps1 -DisableContentCapture
```

`ModelCapacity` is in the quota units reported for the selected deployment SKU,
not an assumed tokens-per-minute conversion. If the exact model/version cannot
be deployed, the script stops with reasons. Select an available alternative
yourself and explicitly supply both `-ModelName` and `-ModelVersion`; there is
no automatic model downgrade.

The script deploys the [foundation](infra/main.bicep), builds an immutable image
tag in ACR, then deploys the [app](infra/app.bicep). No placeholder image is used.
Each Bicep phase runs ARM validation and a what-if preview before provisioning.
The PowerShell entry point forces UTF-8 for Azure CLI's Python output so Unicode
build logs can be streamed on Windows.
The same group/prefix updates the existing deployment and preserves Cosmos data.
An existing OpenAI account's location is retained, not silently moved. Azure API,
authorization and networking failures stop execution instead of being mistaken
for model unavailability.

The final output includes the application URL, model location, Application
Insights link and service endpoints. The script checks app readiness, but does
not submit model requests or verify portal ingestion on your behalf.

### If deployment stops

- **Model/quota unavailable:** request access/quota or explicitly choose another
  model/version. No infrastructure is created when model preflight fails.
- **Role assignment permission denied:** obtain the required RBAC permissions.
- **ACR build failure:** inspect its build output, task permissions, and mirror/
  registry connectivity. Rerunning uses a new image tag; no public PyPI fallback
  is added automatically.
- **Revision not ready:** inspect Container App revisions and console/system logs.
  New managed identity role assignments can take time to propagate. The backend
  refuses readiness when it cannot read its Cosmos container.
- **Model inference fails after rollout:** use the turn's trace ID. Readiness
  checks Cosmos connectivity, not a paid model call.

No automatic deletion/recreation is performed to recover from failures.

## Local development

Python 3.12-3.14, Node.js 22+ and [uv](https://docs.astral.sh/uv/) are required.
Python dependencies are locked in [backend/uv.lock](backend/uv.lock).
The sole Python index configured in [pyproject.toml](backend/pyproject.toml) is:

```text
https://packagefeedproxy.microsoft.io/pypi/simple
```

The `jiter<0.18` resolver constraint avoids a missing wheel metadata file in this
mirror. Do not add credentials to project files or to the lockfile. If mirror
authentication is needed, configure it outside the repository.

Use provisioned Azure endpoints and a developer identity with data-plane roles.
No in-memory production fallback or fake model responses are enabled.

From the repository root:

```powershell
Copy-Item .env.example .env
# Fill .env with COSMOS_ENDPOINT and AZURE_OPENAI_ENDPOINT from deployment output.
# Optionally set APPLICATIONINSIGHTS_CONNECTION_STRING to inspect local traces.
az login
uv sync --project backend --frozen --native-tls
Set-Location backend
uv run --no-sync uvicorn app.main:create_app --factory --reload --env-file ..\.env
```

In another terminal:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://127.0.0.1:5173`).
The Vite proxy keeps API requests same-origin. `COOKIE_SECURE=false` is only for
HTTP localhost; Azure sets it to `true`. If testing locally against a different
Azure tenant, ensure the selected developer credential belongs to that tenant.

Without an Application Insights connection string the backend logs an explicit
warning and does not export telemetry. Never commit your `.env`.

## Conversation behavior

- A persistent HttpOnly browser cookie is the anonymous ownership credential;
  the conversation ID alone does not grant history access.
- Refresh resumes the active conversation using the same browser profile.
  Cookies and storage must be enabled. There is no sign-in, global history
  listing, history sidebar or cross-device recovery.
- **New chat** starts another conversation; it does not delete the previous one.
- **Delete chat** explicitly deletes the active conversation's Cosmos documents.
  Delete before starting a new chat if you do not want to retain old data.
- Clearing browser storage/cookies or losing the browser credential prevents
  recovery through this UI. Administrators can remove abandoned partitions
  directly in Cosmos DB. No TTL is configured.
- History is paginated, 20 turns at a time; the UI can load earlier messages.
- A turn is saved as pending, then completed or failed using transactional writes.
  The final-success SSE event is sent only after persistence succeeds.
- Reusing a turn ID is rejected, not silently replayed. A disconnect or ambiguous
  network failure is not automatically retried, to avoid duplicate model charges.
  Refresh history before submitting a new request.
- A stale pending turn can be marked interrupted when history is refreshed after
  250 seconds. Deletion in progress can be retried if storage access failed.
- Deleting Cosmos history **does not purge Application Insights copies**.
  Its workspace retention is separate (30 days in this template).

Default limits:

| Limit | Value |
|---|---|
| Ingredient/follow-up input | 4,000 characters |
| Previous completed turns in model context | 10 |
| Completion tokens per agent | 1,800 |
| Whole workflow deadline | 180 seconds |
| Simultaneous workflows per replica | 2 |
| Chat submissions per browser | 6 per minute |
| Conversation creation per observed client IP | 20 per minute |

Settings are in [.env.example](.env.example). If older turns are excluded from
model context, the UI says so; stored history is not truncated. The model agents
have no browsing or external tools and use bounded structured-output execution.
Food and pairing recommendations are AI-generated, not safety guarantees.

## Inspect multi-agent tracing in Application Insights

1. Open the deployed app. Enter `salmon, asparagus, lemon and potatoes; dinner for two`.
2. Observe Chef -> Wine -> Review progress and the final menu. Copy the displayed
   **trace ID**. Send a follow-up to create a second turn in the same conversation.
3. Open the Application Insights link printed by the deployment script.
4. Open **Agents (Preview)**. Look for **Chef Agent**, **Wine Agent** and
   **Review Agent** as distinct identities.
5. Select an agent and open its traces/Search, then the end-to-end transaction.
   Expect one HTTP request and recipe workflow with three sequential
   `invoke_agent` operations, each containing its model call.
6. With content capture enabled, inspect input/output messages and available
   input/output token usage. All three agents must share a trace ID for one turn.
7. Use Search or Logs to find the copied trace ID as `operation_Id`.
8. Use the queries in [observability/queries.kql](observability/queries.kql) to
   group multiple turn traces by `gen_ai.conversation.id` / `session_Id`.

Trace layout (framework internals may add child spans):

```text
POST /api/conversations/{conversation_id}/chat
  recipe workflow
    invoke_agent Chef Agent
      chat gpt-6-luna
    invoke_agent Wine Agent
      chat gpt-6-luna
    invoke_agent Review Agent
      chat gpt-6-luna
    Cosmos persistence
```

Each turn has a different trace ID. Conversation ID and session correlation stay
the same across turns. Token totals should be computed from model spans, not by
summing every parent agent span. No token prices or costs are fabricated.

The native Microsoft instrumentation generates agent identities and conversation
messages from LangChain agents. A correlation span processor propagates
conversation, session and turn attributes to child model/dependency spans.
Each specialist restores its caller's OpenTelemetry context on exit, keeping the
three sequential agent spans as siblings rather than nesting under completed agents.
FastAPI is explicitly instrumented once; automatic duplicate FastAPI/OpenAI
instrumentation is avoided. Sampling is 100% for this demo.

GenAI content capture uses `CAPTURE_MESSAGE_CONTENT` plus the distro's documented
GenAI settings. `-DisableContentCapture` disables prompt/output capture in traces,
not Cosmos history. Hidden reasoning and browser credentials are not intentionally
captured. Full-content tracing multiplies storage/ingestion volume, especially
when requests include conversation context.

**Agent UI and GenAI instrumentation are preview/experimental features.** SDK
compatibility is locked and local span shape is tested, but native portal rendering
and Azure ingestion require a deployed run. If agents do not appear, widen the
portal time range, check the Application Insights connection, inspect ingestion/
exporter errors in container logs, and query dependencies for
`gen_ai.operation.name == "invoke_agent"`.

Official references:

- [Application Insights agent monitoring](https://learn.microsoft.com/en-us/azure/azure-monitor/app/agents-view)
- [Tracing LangChain agents](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-framework)
- [Model availability](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)

## Focused validation

From the repository root:

```powershell
uv run --project backend --no-sync pytest backend\tests\test_demo.py -q
uv run --project backend --no-sync ruff check backend
uv run --project backend --no-sync ty check backend\app
Set-Location frontend
npm run build
Set-Location ..
az bicep build --file infra\main.bicep --stdout | Out-Null
az bicep build --file infra\app.bicep --stdout | Out-Null
```

The small backend suite uses mocked Cosmos and a mock HTTP transport for actual
LangChain/Azure OpenAI SDK calls. It checks sequential handoffs, follow-up context,
ownership/deletion, failures/duplicates/limits, Cosmos concurrency/pagination,
exact agent/model span shapes, token usage and the content-capture switch.
It never calls a live model or deploys resources. Wider testing, live model
compatibility, cloud deployment and native portal validation are left to you.

## Costs and cleanup

There are ongoing costs for one running replica, ACR Basic, Cosmos storage,
model calls and telemetry ingestion. Cosmos history is indefinite until deleted;
100% tracing and full prompts can increase ingestion quickly. Set Azure budgets
and remove the demo when finished.

For complete cleanup, review the resource group's contents in Azure and explicitly
delete that group yourself. The deployment script never deletes a resource group.

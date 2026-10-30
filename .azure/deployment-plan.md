# Recipe agents deployment

Status: Validated

## Approved scope

One public Container App serves React and FastAPI. Chef, Wine and Review LangChain
agents run sequentially. Cosmos DB stores anonymous conversations until deletion.
Application Insights records correlated agent/model spans and demo message content.

## Azure context

- Subscription: current Azure CLI selection at script execution; never switched.
- Resource group: one user-selectable group for every resource.
- Primary location: Switzerland North.
- Model fallback: Sweden Central only; exact `gpt-6-luna`, Global Standard.
- Tags: `SecurityControl: Ignore` on the group and taggable resources.
- Deployment is now explicitly authorized by the user in the current subscription.
- Confirmed subscription: a5cafb25-f61a-4eff-bf67-55e603c522a6.
- Resource group: rg-recipe-agents-demo; model quota permits the requested 10 units
  in Switzerland North.

## Delivery

Recipe: AZCLI + Bicep

Bicep foundation and app templates, ACR image build, and a PowerShell/Azure CLI
entry point. Managed identity accesses OpenAI, Cosmos and ACR. Model access,
availability and quota are checked before resource creation.

## Validation

Validation is phased because the remote ACR builder is provisioned by the
foundation. The foundation has passed all applicable checks. App image creation
and application-template validation must pass before the Container App is deployed.

- Core validation: local Bicep builds pass; live ARM validate/what-if and
  subscription authentication/availability checks are deferred to the user.
- Docker build: ACR run dh1 succeeded; immutable image built and pushed.
- Azure Policy validation: inherited assignments reviewed and both ARM validations passed.
- Application build/type checks and six focused backend tests: passed.

Focused backend tests, frontend build/type-check, Bicep lint/build, PowerShell
syntax validation. Native Application Insights rendering, model inference and
cloud rollout require the user's deployment and documented manual checks.

## Preparation results

- Application, native LangChain agents, Cosmos persistence and React UI implemented.
- Six focused backend tests pass, including exact agent/model span shape, usage
  and the telemetry content-capture opt-out.
- Frontend production build/type-check and both Bicep builds pass.
- PowerShell syntax and a mocked CLI preflight verify unavailable models stop
  before resource creation and both approved regions are checked.
- Python dependencies use the requested Microsoft package proxy, with a lockfile.
- No live Azure provisioning, image build or model inference has been performed.
- Live cloud validation/deployment is authorized. Validate foundation before
  provisioning, build remotely in ACR before deploying the application, then
  verify the live app and telemetry. Do not claim portal ingestion before querying it.

## Role Assignment Verification

Status: Static assignments verified.

The app's user-assigned identity has OpenAI User on the model account, Cosmos
DB built-in data contributor on the Cosmos account and AcrPull on its registry.
Developer data access is opt-in through an explicit principal ID. These are
resource-scoped data-plane roles, not broad management-plane grants.

## Section 7: Validation Proof

- `uv run --project backend --no-sync pytest backend\tests\test_demo.py -q`:
  six tests passed.
- `uv run --project backend --no-sync ruff check backend`: passed.
- `uv run --project backend --no-sync ty check backend\app`: passed.
- `python -m compileall` through the backend environment: passed.
- `npm run build` in the frontend: passed (TypeScript plus Vite).
- `az bicep build` for both templates: passed without template diagnostics.
- PowerShell AST parse: passed. Mocked Azure CLI preflight checks both approved
  regions and stops without mutations when the exact model is unavailable.
- Mocked successful deployment: Sweden fallback, quota checks, immutable ACR build,
  separate foundation/app phases and readiness polling passed without Azure changes.
- Azure SDK catalogue schema inspected locally: model.skus, usageName and
  capacity minimum/maximum/step/allowedValues fields confirmed.
- Local Docker daemon unavailable. No claim of a completed image build, live
  ARM/policy validation, actual model inference, or portal ingestion is made.

### Live foundation validation, 2026-10-30

- Current CLI subscription and regions explicitly confirmed with the user.
- Exact model/version listed in both approved regions; Switzerland North quota
  250 units with 100 used, sufficient for this deployment's 10 units.
- Required providers registered and current caller has provisioning/RBAC rights.
- Inherited policy assignments inspected; no applicable deny policy found.
- Approved empty resource group created in Switzerland North with required tag.
- Official Azure validation helper: CLI/auth/build/ARM validate/what-if all PASS.
- Foundation what-if: 14 creates, zero modifications, zero deletions.
- Image build and application/telemetry checks remain pending; no claim of
  completed application validation.

### Live application validation, 2026-10-30

- ACR run dh1 succeeded after 74 seconds. Image tag:
  `20261030144954-e9704b6d`.
- Image digest:
  `sha256:225e23cbdccd30eea2c1b540dee64c96c3f4930f39f5bd3b8b84c021de65a66c`.
- Official application validation helper: CLI/auth/build/ARM validate/what-if all PASS.
- Application what-if reported creates only, with no modifications or deletions.
- Live RBAC confirmed: app identity has AcrPull, OpenAI User and Cosmos
  built-in data contributor at the intended resource scopes.
- Fixed Windows MSI Azure CLI log encoding by explicitly starting its embedded
  Python with `-X utf8`; encoding environment variables alone are ignored by
  the launcher's isolated mode.
- Application rollout and live end-to-end/ingestion checks remain pending.

## Live deployment verification, 2026-10-30

Status: Deployed and verified.

- Application:
  https://recipeagents-app.redmoss-90093d35.switzerlandnorth.azurecontainerapps.io
- Resource group: rg-recipe-agents-demo, in the confirmed subscription.
- Azure OpenAI model: gpt-6-luna, version 2026-09-22, GlobalStandard, 10 units,
  Switzerland North. No regional or model fallback was needed.
- Final ACR run: dh2, succeeded. Image tag: 20261030145947-921c09ae.
- Final digest:
  sha256:9b1d17da838adf161729d247f8914448918b8f12013482d6931d8634b89fdedc.
- Full PowerShell deployment script completed successfully with the Windows
  UTF-8 CLI correction. Both ARM validation/what-if phases passed.
- Container App provisioning succeeded and readiness returned ready.
- Browser UI generated a complete menu and reloaded the saved history.
- API smoke verified completed Cosmos history and 404 for unauthorized access.
- Verified final Application Insights trace: cd23facc4b09be77e4256de20a85189f.
- Three distinct agent spans share workflow parent f0bfaae982c2e0c9 and session
  24bd92cc-6f05-4605-9586-0208041577d4. Each model call is a direct child of its agent.
- All three agents have captured inputs/outputs. The three model calls report
  1,839 input and 1,292 output tokens in total.
- Live tracing exposed callback-context leakage between sequential agents.
  Fixed by restoring caller context per invocation, added a sibling-parent
  regression assertion, passed all six tests/lint/types, rebuilt and verified live.
- Azure's automatic Failure Anomalies alert is globally scoped but belongs to
  the same group; the application and all regional service resources are in
  Switzerland North. The deployment script tags this platform companion too.

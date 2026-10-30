#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidatePattern('^[a-zA-Z0-9._()-]{1,90}$')]
    [string] $ResourceGroup = 'rg-recipe-agents-demo',
    [ValidatePattern('^[a-z][a-z0-9]{2,15}$')]
    [string] $NamePrefix = 'recipeagents',
    [string] $ModelName = 'gpt-6-luna',
    [string] $ModelVersion = '2026-09-22',
    [ValidateRange(1, 1000)]
    [int] $ModelCapacity = 10,
    [ValidatePattern('^$|^[0-9a-fA-F-]{36}$')]
    [string] $DeveloperPrincipalId = '',
    [switch] $DisableContentCapture
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path $PSScriptRoot -Parent
$azureExecutable = (Get-Command az -ErrorAction Stop).Source
$azurePrefix = @()
if ($IsWindows -and [IO.Path]::GetExtension($azureExecutable) -eq '.cmd') {
    $embeddedPython = Join-Path (Split-Path (Split-Path $azureExecutable -Parent) -Parent) 'python.exe'
    if (Test-Path -LiteralPath $embeddedPython) {
        # The MSI launcher uses Python isolated mode, which ignores encoding environment variables.
        $azureExecutable = $embeddedPython
        $azurePrefix = @('-X', 'utf8', '-IBm', 'azure.cli')
    }
}

function Invoke-AzureJson {
    param([string[]] $Arguments)
    $result = & $azureExecutable @azurePrefix @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed: az $($Arguments -join ' '). Resolve the reported error before retrying."
    }
    return (($result -join "`n") | ConvertFrom-Json -AsHashtable -NoEnumerate)
}

function Invoke-Azure {
    param([string[]] $Arguments)
    & $azureExecutable @azurePrefix @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments -join ' '). Deployment was not completed."
    }
}

function Test-AllowedAction {
    param([object[]] $Permissions, [string] $Action)
    foreach ($permission in $Permissions) {
        $allowed = @($permission.actions | Where-Object { $Action -like $_ }).Count -gt 0
        $excluded = @($permission.notActions | Where-Object { $Action -like $_ }).Count -gt 0
        if ($allowed -and -not $excluded) { return $true }
    }
    return $false
}

$account = Invoke-AzureJson @('account', 'show')
if ($account.state -ne 'Enabled') { throw 'The selected Azure subscription is not enabled.' }
$subscription = $account.id
Write-Host "Using current Azure CLI subscription: $($account.name) ($subscription)"
Write-Host "Resource group: $ResourceGroup; primary location: Switzerland North"
Write-Warning 'This is a public, billable demo. Conversation data is retained until deleted.'
if (-not $DisableContentCapture) {
    Write-Warning 'Ingredient requests and model outputs will also be recorded in Application Insights.'
}

$providers = @(
    'Microsoft.App', 'Microsoft.ContainerRegistry', 'Microsoft.CognitiveServices',
    'Microsoft.DocumentDB', 'Microsoft.Insights', 'Microsoft.OperationalInsights',
    'Microsoft.ManagedIdentity'
)
foreach ($provider in $providers) {
    $registration = Invoke-AzureJson @('provider', 'show', '--namespace', $provider, '--subscription', $subscription)
    if ($registration.registrationState -ne 'Registered') {
        throw "Register $provider first: az provider register --namespace $provider --subscription $subscription --wait"
    }
}

$groupExists = Invoke-AzureJson @('group', 'exists', '--name', $ResourceGroup, '--subscription', $subscription)
$scope = "/subscriptions/$subscription"
if ($groupExists) { $scope += "/resourceGroups/$ResourceGroup" }
$permissions = Invoke-AzureJson @(
    'rest', '--method', 'get', '--url',
    "https://management.azure.com$scope/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
)
foreach ($action in @(
    'Microsoft.Resources/deployments/write',
    'Microsoft.Authorization/roleAssignments/write',
    'Microsoft.DocumentDB/databaseAccounts/write',
    'Microsoft.CognitiveServices/accounts/write',
    'Microsoft.App/containerApps/write',
    'Microsoft.ContainerRegistry/registries/scheduleRun/action'
)) {
    if (-not (Test-AllowedAction $permissions.value $action)) {
        throw "The selected identity lacks $action at $scope. Contributor alone cannot create role assignments."
    }
}

$existing = @()
if ($groupExists) {
    $existing = @(
        (Invoke-AzureJson @('cognitiveservices', 'account', 'list', '--resource-group', $ResourceGroup, '--subscription', $subscription)) |
        Where-Object { $_.name -like "$NamePrefix-openai-*" }
    )
}
if ($existing.Count -gt 1) { throw 'Multiple matching OpenAI accounts exist; use an unambiguous resource group/prefix.' }
$regions = @('switzerlandnorth', 'swedencentral')
if ($existing.Count -eq 1) {
    $regions = @($existing[0].location.ToLowerInvariant().Replace(' ', ''))
    if ($regions[0] -notin @('switzerlandnorth', 'swedencentral')) {
        throw 'The existing OpenAI resource is outside the two approved regions.'
    }
    Write-Host "Keeping the existing model account in $($regions[0]); resources are never moved automatically."
}

$modelLocation = $null
$reasons = [System.Collections.Generic.List[string]]::new()
foreach ($region in $regions) {
    $catalog = Invoke-AzureJson @('cognitiveservices', 'model', 'list', '--location', $region, '--subscription', $subscription)
    $matches = @($catalog | Where-Object {
        $_.model.name -eq $ModelName -and $_.model.version -eq $ModelVersion -and $_.model.format -eq 'OpenAI'
    })
    if ($matches.Count -eq 0) {
        $reasons.Add("${region}: exact model/version is not offered to this subscription.")
        continue
    }
    $skus = @($matches[0].model.skus | Where-Object { $_.name -eq 'GlobalStandard' })
    if ($skus.Count -eq 0) {
        $reasons.Add("${region}: GlobalStandard is unavailable for this model.")
        continue
    }
    $sku = $skus[0]
    $capacity = $sku.capacity
    $allowed = $true
    if ($capacity.ContainsKey('allowedValues') -and $null -ne $capacity.allowedValues -and
        @($capacity.allowedValues).Count -gt 0) {
        $allowed = $ModelCapacity -in $capacity.allowedValues
    }
    elseif (($null -ne $capacity.minimum -and $ModelCapacity -lt $capacity.minimum) -or
        ($null -ne $capacity.maximum -and $ModelCapacity -gt $capacity.maximum) -or
        ($null -ne $capacity.step -and $capacity.step -gt 0 -and
            ($ModelCapacity - $capacity.minimum) % $capacity.step -ne 0)) {
        $allowed = $false
    }
    if (-not $allowed) {
        $reasons.Add("${region}: capacity $ModelCapacity is outside this model's supported capacity range/step.")
        continue
    }
    if (-not $sku.usageName) { throw "Model catalogue in $region did not supply a quota usage name." }
    $usages = Invoke-AzureJson @('cognitiveservices', 'usage', 'list', '--location', $region, '--subscription', $subscription)
    $quota = @($usages | Where-Object { $_.name.value -eq $sku.usageName })
    if ($quota.Count -ne 1) {
        $reasons.Add("${region}: required quota $($sku.usageName) is not assigned.")
        continue
    }
    $credit = 0
    if ($existing.Count -eq 1) {
        $deployments = Invoke-AzureJson @(
            'cognitiveservices', 'account', 'deployment', 'list', '--name', $existing[0].name,
            '--resource-group', $ResourceGroup, '--subscription', $subscription
        )
        foreach ($deployment in $deployments) {
            if ($deployment.name -eq $ModelName -and $deployment.sku.name -eq 'GlobalStandard' -and
                $deployment.properties.model.name -eq $ModelName) {
                $credit = $deployment.sku.capacity
            }
        }
    }
    $available = $quota[0].limit - $quota[0].currentValue + $credit
    if ($available -lt $ModelCapacity) {
        $reasons.Add("${region}: quota has $available units available; $ModelCapacity required.")
        continue
    }
    $modelLocation = $region
    break
}
if (-not $modelLocation) {
    throw "Cannot deploy $ModelName/$ModelVersion. No resources were created. Ask for an available model or quota; no substitution was made.`n$($reasons -join "`n")"
}
Write-Host "Selected exact model $ModelName/$ModelVersion in $modelLocation (GlobalStandard, $ModelCapacity capacity units)."
foreach ($reason in $reasons) { Write-Warning $reason }
Write-Warning 'GlobalStandard inference may be processed outside the Azure resource region.'

foreach ($template in @('main.bicep', 'app.bicep')) {
    & $azureExecutable @azurePrefix bicep build --file (Join-Path $root "infra\$template") --stdout --only-show-errors | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Bicep build failed: $template" }
}
if ($groupExists) {
    Invoke-Azure @('group', 'update', '--name', $ResourceGroup, '--set', 'tags.SecurityControl=Ignore',
        '--subscription', $subscription, '--output', 'none')
}
else {
    Invoke-Azure @('group', 'create', '--name', $ResourceGroup, '--location', 'switzerlandnorth',
        '--tags', 'SecurityControl=Ignore', '--subscription', $subscription, '--output', 'none')
}

$foundationArguments = @(
    '--name', "$NamePrefix-foundation",
    '--resource-group', $ResourceGroup, '--subscription', $subscription,
    '--template-file', (Join-Path $root 'infra\main.bicep'),
    '--parameters', "namePrefix=$NamePrefix", "modelLocation=$modelLocation",
    "modelName=$ModelName", "modelVersion=$ModelVersion", "modelCapacity=$ModelCapacity",
    "developerPrincipalId=$DeveloperPrincipalId"
)
Invoke-Azure (@('deployment', 'group', 'validate') + $foundationArguments + @('--output', 'none'))
Invoke-Azure (@('deployment', 'group', 'what-if') + $foundationArguments + @('--result-format', 'ResourceIdOnly'))
$foundation = Invoke-AzureJson (@('deployment', 'group', 'create') + $foundationArguments)
$outputs = $foundation.properties.outputs
$anomalyRules = Invoke-AzureJson @(
    'resource', 'list', '--resource-group', $ResourceGroup, '--subscription', $subscription,
    '--resource-type', 'Microsoft.AlertsManagement/smartDetectorAlertRules',
    '--query', "[?name=='Failure Anomalies - $($outputs.insightsName.value)'].id"
)
foreach ($rule in $anomalyRules) {
    Invoke-Azure @('tag', 'update', '--resource-id', $rule, '--operation', 'Merge',
        '--tags', 'SecurityControl=Ignore', '--subscription', $subscription, '--output', 'none')
}
$imageTag = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
Invoke-Azure @(
    'acr', 'build', '--registry', $outputs.registryName.value, '--subscription', $subscription,
    '--image', "recipe-agents:$imageTag", '--file', (Join-Path $root 'Dockerfile'),
    '--platform', 'linux/amd64', $root
)

$applicationArguments = @(
    '--name', "$NamePrefix-application",
    '--resource-group', $ResourceGroup, '--subscription', $subscription,
    '--template-file', (Join-Path $root 'infra\app.bicep'),
    '--parameters', "namePrefix=$NamePrefix", "imageTag=$imageTag",
    "registryName=$($outputs.registryName.value)", "identityName=$($outputs.identityName.value)",
    "environmentName=$($outputs.environmentName.value)", "insightsName=$($outputs.insightsName.value)",
    "cosmosName=$($outputs.cosmosName.value)", "openaiName=$($outputs.openaiName.value)",
    "deploymentName=$($outputs.deploymentName.value)",
    "captureMessageContent=$((-not $DisableContentCapture).ToString().ToLowerInvariant())"
)
Invoke-Azure (@('deployment', 'group', 'validate') + $applicationArguments + @('--output', 'none'))
Invoke-Azure (@('deployment', 'group', 'what-if') + $applicationArguments + @('--result-format', 'ResourceIdOnly'))
$application = Invoke-AzureJson (@('deployment', 'group', 'create') + $applicationArguments)
$url = $application.properties.outputs.appUrl.value
$healthy = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $health = Invoke-RestMethod "$url/health/ready" -TimeoutSec 10
        if ($health.status -eq 'ready') { $healthy = $true; break }
    }
    catch [Microsoft.PowerShell.Commands.HttpResponseException],
          [System.Net.Http.HttpRequestException], [System.Threading.Tasks.TaskCanceledException] {
        Write-Host "Waiting for revision readiness ($($attempt + 1)/40): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 10
}
if (-not $healthy) {
    throw "The app did not become ready. Inspect Container App revisions/system logs and identity role propagation. Resource group: $ResourceGroup"
}
Write-Host "`nApplication: $url"
Write-Host "Resource group: $ResourceGroup"
Write-Host "Model: $ModelName/$ModelVersion in $modelLocation"
Write-Host "Application Insights: https://portal.azure.com/#resource$($outputs.insightsId.value)/overview"
Write-Host "COSMOS_ENDPOINT=$($outputs.cosmosEndpoint.value)"
Write-Host "AZURE_OPENAI_ENDPOINT=$($outputs.openaiEndpoint.value)"
Write-Host 'Open README.md for the agent tracing walkthrough and separate data retention notes.'

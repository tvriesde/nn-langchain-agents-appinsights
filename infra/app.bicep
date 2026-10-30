targetScope = 'resourceGroup'

param namePrefix string = 'recipeagents'
param location string = 'switzerlandnorth'
param registryName string
param identityName string
param environmentName string
param insightsName string
param cosmosName string
param openaiName string
param deploymentName string = 'gpt-6-luna'
param imageTag string
param captureMessageContent bool = true
param tags object = {
  SecurityControl: 'Ignore'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: insightsName
}
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosName
}
resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: openaiName
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-app'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'applicationinsights'
          value: insights.properties.ConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'recipes'
          image: '${registry.properties.loginServer}/recipe-agents:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openai.properties.endpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: deploymentName }
            { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
            { name: 'COSMOS_DATABASE', value: 'recipes' }
            { name: 'COSMOS_CONTAINER', value: 'conversations' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'applicationinsights' }
            { name: 'CAPTURE_MESSAGE_CONTENT', value: string(captureMessageContent) }
            { name: 'COOKIE_SECURE', value: 'true' }
            { name: 'OTEL_SERVICE_NAME', value: 'recipe-agents' }
            { name: 'OTEL_RESOURCE_ATTRIBUTES', value: 'service.version=${imageTag}' }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 5
              failureThreshold: 40
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000 }
              periodSeconds: 30
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appName string = app.name

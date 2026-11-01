targetScope = 'resourceGroup'

param location string = resourceGroup().location
param tags object = { ManagedBy: 'bicep', Workload: 'teams-transcripts-sample' }
param organizers object
@description('Signed-in deployment operator object ID; no personal default.')
param operatorPrincipalId string
param appClientId string = ''
param appPrincipalId string = ''
param graphChangeTrackingPrincipalId string = ''
param deployFunction bool = false
@allowed(['disabled', 'enabled', 'rollback'])
param reconciliationMode string = 'disabled'
param ingestionEnabled bool = false
param enableFreeTier bool = false
param alertEmail string = ''
param eventHubName string = 'teams-transcripts'
param chatModelName string = 'gpt-6-luna'
param chatModelVersion string
param chatDeploymentName string = 'chat'
@allowed(['GlobalStandard', 'Standard'])
param chatModelSku string = 'GlobalStandard'
@minValue(1)
param chatModelCapacity int = 10
param embeddingModelVersion string = '1'
param embeddingModelSku string = 'GlobalStandard'
param embeddingModelCapacity int = 10

var workloadTags = union(tags, { Workload: 'teams-transcripts', Criticality: 'Tier2' })
var suffix = uniqueString(resourceGroup().id)
var functionName = 'fn-teams-transcripts-${suffix}'
var roleStorageBlobDataOwnerDefinitionId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var roleStorageQueueDataContributorDefinitionId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var roleStorageAccountContributorDefinitionId = '17d1049b-9a84-46fb-8f53-869881c3d3ab'
var roleKeyVaultSecretsUserDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'
var roleKeyVaultSecretsOfficerDefinitionId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var hostStorageRoles = [
  roleStorageBlobDataOwnerDefinitionId
  roleStorageQueueDataContributorDefinitionId
  roleStorageAccountContributorDefinitionId
]

/*### purpose: dedicated identity for Function host, archive and app federation. ###*/
resource functionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-teams-transcripts-${suffix}'
  location: location
  tags: workloadTags
}

/*### purpose: small Entra-only private archive and separate operational/deployment containers. ###*/
resource storageAccount 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: 'stteams${suffix}'
  location: location
  tags: workloadTags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // Flex and Graph receive over Entra-authenticated public service endpoints.
    publicNetworkAccess: 'Enabled'
    accessTier: 'Hot'
    networkAcls: { defaultAction: 'Allow', bypass: 'None' }
  }
}

/*### purpose: no versioning or soft-delete copies beyond the archive lifecycle. ###*/
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    isVersioningEnabled: false
    deleteRetentionPolicy: { enabled: false }
    containerDeleteRetentionPolicy: { enabled: false }
  }
}

/*### purpose: isolated seven-day events/quarantine; state and host checkpoints are separate. ###*/
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [for containerName in ['events', 'quarantine', 'operations', 'deployment'] : {
  parent: blobService
  name: containerName
  properties: { publicAccess: 'None' }
}]

/*### purpose: ingestion doorbell, with identifiers only and immutable expiry in every message. ###*/
resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
}
/*### purpose: dedicated ingestion and Functions-managed poison queue. ###*/
resource ingestQueues 'Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01' = [for queueName in ['transcript-ingest', 'transcript-ingest-poison']: {
  parent: queueService
  name: queueName
}]

/*### purpose: Function may write only the transcript database. ###*/
module transcriptContributor 'cosmos-role.bicep' = {
  name: '${suffix}-teams-transcripts-contributor'
  params: {
    cosmosAccountName: cosmosAccount.name
    databaseName: teamsTranscriptsDatabase.name
    principalId: functionIdentity.properties.principalId
    access: 'contributor'
  }
}

/*### purpose: Azure Functions identity-based host storage and Flex deployment access. ###*/
resource hostStorageGrants 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in hostStorageRoles: {
  scope: storageAccount
  name: guid(storageAccount.id, functionIdentity.id, roleId)
  properties: {
    principalId: functionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]

/*### purpose: delete archived and quarantined blobs after seven days; state is excluded. ###*/
resource retention 'Microsoft.Storage/storageAccounts/managementPolicies@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'teams-notification-seven-days'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['events/', 'quarantine/'] }
            actions: { baseBlob: { delete: { daysAfterCreationGreaterThan: 7 } } }
          }
        }
      ]
    }
  }
}

/*### purpose: dedicated Key Vault and scope identity access to the generated clientState secret. ###*/
resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: 'kvteams${suffix}'
  location: location
  tags: workloadTags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}
/*### purpose: secret value is generated outside ARM and never emitted in deployment outputs. ###*/
resource clientStateSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: keyVault
  name: 'teams-transcripts-client-state'
}
/*### purpose: only the Function identity can resolve its Key Vault reference. ###*/
resource secretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: clientStateSecret
  name: guid(clientStateSecret.id, functionIdentity.id, roleKeyVaultSecretsUserDefinitionId)
  properties: {
    principalId: functionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUserDefinitionId)
  }
}

/*### purpose: operator bootstraps only this verification secret. ###*/
resource secretBootstrap 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: clientStateSecret
  name: guid(clientStateSecret.id, operatorPrincipalId, roleKeyVaultSecretsOfficerDefinitionId)
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsOfficerDefinitionId)
  }
}

/*### purpose: dedicated embedding API credential; no key in ARM settings or outputs. ###*/
resource embeddingKey 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: keyVault
  name: 'teams-transcripts-embedding-key'
}
/*### purpose: Function resolves only its dedicated embedding secret. ###*/
resource embeddingKeyReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: embeddingKey
  name: guid(embeddingKey.id, functionIdentity.id, roleKeyVaultSecretsUserDefinitionId)
  properties: {
    principalId: functionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUserDefinitionId)
  }
}
/*### purpose: operator may bootstrap only the dedicated embedding secret. ###*/
resource embeddingKeyBootstrap 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: embeddingKey
  name: guid(embeddingKey.id, operatorPrincipalId, roleKeyVaultSecretsOfficerDefinitionId)
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsOfficerDefinitionId)
  }
}

/*### purpose: dedicated workspace-backed Application Insights. ###*/
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-teams-${suffix}'
  location: location
  tags: workloadTags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-teams-${suffix}'
  location: location
  tags: workloadTags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: workspace.id, RetentionInDays: 30 }
}

/*### purpose: dedicated Flex Consumption plan, zero always-ready instances. ###*/
resource functionPlan 'Microsoft.Web/serverfarms@2024-11-01' = if (deployFunction) {
  name: 'plan-teams-transcripts-${suffix}'
  location: location
  tags: workloadTags
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

/*### purpose: Python 3.12 Function; managed identity host and deployment storage. ###*/
resource functionApp 'Microsoft.Web/sites@2024-11-01' = if (deployFunction) {
  name: functionName
  location: location
  tags: workloadTags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${functionIdentity.id}': {} }
  }
  properties: {
    serverFarmId: functionPlan!.id
    httpsOnly: true
    keyVaultReferenceIdentity: functionIdentity.id
    // SCM OneDeploy requires service ingress; the package exposes no HTTP functions.
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      runtime: { name: 'python', version: '3.12' }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 512 }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}deployment'
          authentication: { type: 'UserAssignedIdentity', userAssignedIdentityResourceId: functionIdentity.id }
        }
      }
    }
    siteConfig: { minTlsVersion: '1.2', ftpsState: 'Disabled' }
  }
  dependsOn: [hostStorageGrants, secretReader, containers]
}

/*### purpose: explicit app settings; no storage key, connection-string credential or app secret. ###*/
resource functionSettings 'Microsoft.Web/sites/config@2024-11-01' = if (deployFunction) {
  parent: functionApp
  name: 'appsettings'
  properties: {
    AZURE_CLIENT_ID: functionIdentity.properties.clientId
    AzureWebJobsStorage__accountName: storageAccount.name
    AzureWebJobsStorage__credential: 'managedidentity'
    AzureWebJobsStorage__clientId: functionIdentity.properties.clientId
    TeamsEventHub__fullyQualifiedNamespace: '${eventHubNamespace.name}.servicebus.windows.net'
    TeamsEventHub__credential: 'managedidentity'
    TeamsEventHub__clientId: functionIdentity.properties.clientId
    APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
    TEAMS_ORGANIZERS: string(organizers)
    TEAMS_APP_CLIENT_ID: appClientId
    TEAMS_CLIENT_STATE: '@Microsoft.KeyVault(SecretUri=${keyVault.properties.vaultUri}secrets/${clientStateSecret.name}/)'
    TEAMS_STORAGE_BLOB_ENDPOINT: storageAccount.properties.primaryEndpoints.blob
    TEAMS_EVENT_HUB_NAME: eventHubName
    TEAMS_NOTIFICATION_URL: 'EventHub:https://${eventHubNamespace.name}.servicebus.windows.net/eventhubname/${eventHubName}?tenantId=${subscription().tenantId}'
    TEAMS_RECONCILIATION_MODE: reconciliationMode
    TEAMS_INGESTION_ENABLED: ingestionEnabled && reconciliationMode == 'enabled' ? 'true' : 'false'
    TEAMS_STORAGE_QUEUE_ENDPOINT: storageAccount.properties.primaryEndpoints.queue
    TEAMS_COSMOS_ENDPOINT: 'https://${cosmosAccount.name}.documents.azure.com:443/'
    TEAMS_DATABASE: teamsTranscriptsDatabase.name
    TEAMS_CONTAINER: teamsTranscriptsContainer.name
    TEAMS_RETENTION_DAYS: '7'
    TEAMS_EMBEDDING_ENDPOINT: 'https://${foundryAccount.name}.services.ai.azure.com'
    TEAMS_EMBEDDING_DEPLOYMENT: 'text-embedding-3-large'
    TEAMS_EMBEDDING_KEY: '@Microsoft.KeyVault(SecretUri=${keyVault.properties.vaultUri}secrets/${embeddingKey.name}/)'
    'AzureWebJobs.transcript_ingest.Disabled': ingestionEnabled && reconciliationMode == 'enabled' ? 'false' : 'true'
    'AzureWebJobs.transcript_notifications.Disabled': reconciliationMode == 'enabled' ? 'false' : 'true'
  }
}

/*### purpose: report expiry, heartbeat loss, failures and processing delay. ###*/
module monitoring 'monitoring.bicep' = if (deployFunction) {
  name: '${suffix}-teams-transcripts-monitoring'
  params: {
    location: location
    tags: workloadTags
    organizerIds: organizers.organizerIds
    alertEmail: alertEmail
    appInsightsId: appInsights.id
    enabled: reconciliationMode == 'enabled'
  }
}

output functionAppName string = functionName
output storageAccountName string = storageAccount.name
output functionIdentityPrincipalId string = functionIdentity.properties.principalId
output functionIdentityClientId string = functionIdentity.properties.clientId
output appClientId string = appClientId
output appServicePrincipalId string = appPrincipalId

resource teamsTranscriptsDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: cosmosAccount
  name: 'teams-transcripts'
  properties: { resource: { id: 'teams-transcripts' } }
}

/*### purpose: meeting-local canonical pages, manifests and exact hybrid retrieval chunks. ###*/
resource teamsTranscriptsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: teamsTranscriptsDatabase
  name: 'transcripts'
  properties: {
    resource: {
      id: 'transcripts'
      partitionKey: { paths: ['/tenantId', '/organizerId', '/meetingKey'], kind: 'MultiHash', version: 2 }
      defaultTtl: -1
      vectorEmbeddingPolicy: {
        vectorEmbeddings: [{ path: '/embedding', dataType: 'float32', dimensions: 505, distanceFunction: 'cosine' }]
      }
      fullTextPolicy: { defaultLanguage: 'en-US', fullTextPaths: [{ path: '/text', language: 'en-US' }] }
      // ARM supports fullTextIndexes; this Bicep type definition omits it.
      indexingPolicy: any({
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/embedding/*' }, { path: '/utterances/*' }, { path: '/locators/*' }, { path: '/_etag/?' }]
        vectorIndexes: [{ path: '/embedding', type: 'flat' }]
        fullTextIndexes: [{ path: '/text' }]
      })
    }
    options: { autoscaleSettings: { maxThroughput: 1000 } }
  }
}


/*### purpose: dedicated Entra-only corpus with optional free tier at creation. ###*/
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' = {
  name: 'cosmosteams${suffix}'
  location: location
  tags: workloadTags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: enableFreeTier
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [{ locationName: location, failoverPriority: 0, isZoneRedundant: false }]
    capabilities: [{ name: 'EnableNoSQLVectorSearch' }, { name: 'EnableNoSQLFullTextSearch' }]
  }
}

/*### purpose: public image dependency, private images and no registry passwords. ###*/
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrteams${suffix}'
  location: location
  tags: workloadTags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

/*### purpose: direct notification delivery, dedicated Basic namespace and default consumer. ###*/
resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: 'eh-teams-${suffix}'
  location: location
  tags: workloadTags
  sku: { name: 'Basic', tier: 'Basic', capacity: 1 }
  properties: { minimumTlsVersion: '1.2', disableLocalAuth: true }
}
resource transcriptHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: eventHubName
  properties: {
    partitionCount: 2
    status: 'Active'
    retentionDescription: { cleanupPolicy: 'Delete', retentionTimeInHours: 24 }
  }
}
resource senderGrants 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in filter([graphChangeTrackingPrincipalId, appPrincipalId], id => !empty(id)): {
  scope: transcriptHub
  name: guid(transcriptHub.id, principalId, 'sender')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2b629674-e913-4c01-ae53-ef4638d8f975')
  }
}]
resource receiverGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: transcriptHub
  name: guid(transcriptHub.id, functionIdentity.id, 'receiver')
  properties: {
    principalId: functionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde')
  }
}

/*### purpose: Foundry account/project with independent platform conversation storage. ###*/
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'foundryteams${suffix}'
  location: location
  tags: workloadTags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: 'foundryteams${suffix}'
    allowProjectManagement: true
    disableLocalAuth: false
    publicNetworkAccess: 'Enabled'
  }
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: 'teams-transcripts'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: { displayName: 'Teams transcripts', description: 'Organizer-private seven-day transcript assistant' }
}
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: chatDeploymentName
  dependsOn: [embeddingDeployment]
  sku: { name: chatModelSku, capacity: chatModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatModelName, version: chatModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: 'text-embedding-3-large'
  sku: { name: embeddingModelSku, capacity: embeddingModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-large', version: embeddingModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
/*### purpose: permit hosted image execution without giving the project corpus access. ###*/
resource capabilityHost 'Microsoft.CognitiveServices/accounts/capabilityHosts@2025-10-01-preview' = {
  parent: foundryAccount
  name: 'agents'
  properties: { capabilityHostKind: 'Agents', enablePublicHostingEnvironment: true }
}
resource acrConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'hosted-images'
  properties: {
    category: 'ContainerRegistry'
    target: registry.properties.loginServer
    authType: 'ManagedIdentity'
    isSharedToAll: false
    credentials: { clientId: project.identity.principalId, resourceId: project.id }
  }
}
resource projectPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, project.id, 'pull')
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
/*### purpose: enrolled humans can use the playground; runtime scopes each caller. ###*/
resource playgroundUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for organizer in organizers.organizerIds: {
  scope: project
  name: guid(project.id, organizer, 'playground')
  properties: {
    principalId: organizer
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}]

output keyVaultName string = keyVault.name
output cosmosAccountName string = cosmosAccount.name
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output teamsTranscriptsDatabaseName string = teamsTranscriptsDatabase.name
output teamsTranscriptsContainerName string = teamsTranscriptsContainer.name
output teamsTranscriptsRetentionDays int = 7
output foundryAccountName string = foundryAccount.name
output foundryProjectName string = project.name
output foundryProjectEndpoint string = 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${project.name}'
output embeddingEndpoint string = 'https://${foundryAccount.name}.services.ai.azure.com'
output modelDeploymentName string = chatDeployment.name
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output eventHubNamespaceName string = eventHubNamespace.name
output eventHubName string = transcriptHub.name
output appInsightsName string = appInsights.name

/*### purpose: hosted telemetry connection; no account key, SAS or bearer token. ###*/
resource insightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'hosted-monitoring'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: false
    credentials: { key: appInsights.properties.ConnectionString }
    metadata: { ApiType: 'Azure', ResourceId: appInsights.id }
  }
}

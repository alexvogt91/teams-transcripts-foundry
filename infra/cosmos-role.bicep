targetScope = 'resourceGroup'

param cosmosAccountName string
param databaseName string
param principalId string
@allowed(['reader', 'contributor'])
param access string

var roleId = access == 'reader' ? '00000000-0000-0000-0000-000000000001' : '00000000-0000-0000-0000-000000000002'

/*### purpose: existing Entra-only account; never grant the account-wide data plane. ###*/
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = { name: cosmosAccountName }

/*### purpose: dedicated transcript database access; neighboring databases remain inaccessible. ###*/
resource transcriptDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, databaseName, principalId, roleId)
  properties: {
    principalId: principalId
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${roleId}'
    scope: '${cosmosAccount.id}/dbs/${databaseName}'
  }
}

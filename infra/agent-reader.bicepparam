using './cosmos-role.bicep'

// Supplied by the deployment helper after the candidate identity exists.
param cosmosAccountName = 'replace-with-your-account'
param databaseName = 'teams-transcripts'
param principalId = '55555555-5555-4555-8555-555555555555'
param access = 'reader'

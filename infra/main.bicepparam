using './main.bicep'

// Synthetic examples only. scripts/manage.py supplies the actual validated values.
param organizers = loadJsonContent('../configuration/teams-transcript-organizers.example.json')
param operatorPrincipalId = '22222222-2222-4222-8222-222222222222'
param chatModelVersion = 'replace-with-an-available-version'
param deployFunction = false
param reconciliationMode = 'disabled'
param ingestionEnabled = false
param enableFreeTier = false

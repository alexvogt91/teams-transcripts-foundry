param location string
param tags object
param appInsightsId string
param enabled bool
param organizerIds array
param alertEmail string = ''

var alerts = [
  {
    name: 'subscription-expiry'
    query: 'traces | where message startswith "teams_transcripts timer_heartbeat " | extend organizerId=tostring(parse_json(extract("teams_transcripts timer_heartbeat (.*)", 1, message)).organizerId), hours=todouble(parse_json(extract("teams_transcripts timer_heartbeat (.*)", 1, message)).expiryHours) | where hours < 24'
    threshold: 0
    operator: 'GreaterThan'
  }
  {
    name: 'timer-heartbeat'
    query: 'let expected=datatable(organizerId:string) [${join(map(organizerIds, id => '"${id}"'), ',')}]; expected | join kind=leftouter (traces | where message startswith "teams_transcripts timer_heartbeat " | extend organizerId=tostring(parse_json(extract("teams_transcripts timer_heartbeat (.*)", 1, message)).organizerId) | summarize heartbeats=count() by organizerId) on organizerId | project organizerId, heartbeats=coalesce(heartbeats, 0)'
    threshold: 1
    operator: 'LessThan'
  }
  {
    name: 'delivery-failure'
    query: 'traces | where message startswith "teams_transcripts " | where message has_any ("archive_failure", "processing_failure", "ingestion_failure", "reconciliation_failure", "delivery_gap") | extend organizerId=tostring(parse_json(extract("teams_transcripts [^ ]+ (.*)", 1, message)).organizerId)'
    threshold: 0
    operator: 'GreaterThan'
  }
  {
    name: 'processing-delay'
    query: 'traces | where message startswith "teams_transcripts event_archived " | extend organizerId=tostring(parse_json(extract("teams_transcripts event_archived (.*)", 1, message)).organizerId), delay=todouble(parse_json(extract("teams_transcripts event_archived (.*)", 1, message)).processingDelaySeconds) | where delay > 300'
    threshold: 0
    operator: 'GreaterThan'
  }
]

/*### purpose: operational alert delivery to archive owner. ###*/
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: 'teams-transcripts-operations'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'TeamsTx'
    enabled: true
    emailReceivers: [{ name: 'Operator', emailAddress: alertEmail, useCommonAlertSchema: true }]
  }
}

/*### purpose: metadata-only operational signals; no payload or secret in alert queries. ###*/
resource alertRules 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for alert in alerts: {
  name: 'teams-transcripts-${alert.name}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'Teams transcripts: ${alert.name}'
    enabled: enabled
    severity: 2
    scopes: [appInsightsId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT45M'
    criteria: {
      allOf: [{
        query: alert.query
        timeAggregation: alert.name == 'timer-heartbeat' ? 'Minimum' : 'Count'
        metricMeasureColumn: alert.name == 'timer-heartbeat' ? 'heartbeats' : null
        operator: alert.operator
        threshold: alert.threshold
        dimensions: [{ name: 'organizerId', operator: 'Include', values: ['*'] }]
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      }]
    }
    actions: { actionGroups: !empty(alertEmail) ? [actionGroup!.id] : [] }
    autoMitigate: true
  }
}]

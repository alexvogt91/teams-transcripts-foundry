# Offline contract checks; no module, network session or tenant needed.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '../application-access-policy.ps1'
$app = '44444444-4444-4444-8444-444444444444'
$organizer = '22222222-2222-4222-8222-222222222222'
$owner = "teams-transcripts organizer $organizer app $app"
function Assert-True($value, $message) { if (-not $value) { throw $message } }
function Get-CsUserPolicyAssignment {
    param($Identity, $PolicyType)
    Assert-True ($Identity.ToString() -eq $organizer) 'Wrong organizer'
    if ($PolicyType -eq 'TeamsMeetingPolicy') { return [pscustomobject]@{ PolicyName = $global:meetingAssignment } }
    return [pscustomobject]@{ PolicyName = $global:appAssignment }
}
function Get-CsApplicationAccessPolicy {
    param($Identity)
    if ($Identity) { return $global:apps[$Identity -replace '^Tag:', ''] }
    return @($global:apps.Values)
}
function Set-CsApplicationAccessPolicy {
    param($Identity, $AppIds)
    $global:writes++
    $global:apps[$Identity].AppIds = $AppIds
}
function New-CsApplicationAccessPolicy {
    param($Identity, $AppIds, $Description)
    $global:writes++
    $global:apps[$Identity] = [pscustomobject]@{ Identity = "Tag:$Identity"; AppIds = $AppIds; Description = $Description }
}
function Grant-CsApplicationAccessPolicy {
    param($Identity, $PolicyName)
    $global:writes++
    if (-not $global:failAssignment) { $global:appAssignment = $PolicyName }
}
function Get-CsTeamsMeetingPolicy {
    param($Identity)
    if ($Identity) { return $global:meetings[$Identity -replace '^Tag:', ''] }
    return @($global:meetings.Values)
}
function New-CsTeamsMeetingPolicy {
    param($Identity, $Description, $AllowTranscription, $AllowCloudRecording, $AllowIPVideo, $MediaBitRateKb)
    $global:writes++
    $global:meetings[$Identity] = [pscustomobject]@{ Identity = "Tag:$Identity"; Description = $Description; AllowTranscription = $AllowTranscription; AllowCloudRecording = $AllowCloudRecording; AllowIPVideo = $AllowIPVideo; MediaBitRateKb = $MediaBitRateKb }
}
function Set-CsTeamsMeetingPolicy {
    param($Identity, $AllowTranscription, $AllowCloudRecording, $AllowIPVideo, $MediaBitRateKb)
    $global:writes++
    $global:meetings[$Identity].AllowTranscription = $AllowTranscription
    $global:meetings[$Identity].AllowCloudRecording = $AllowCloudRecording
    $global:meetings[$Identity].AllowIPVideo = $AllowIPVideo
    $global:meetings[$Identity].MediaBitRateKb = $MediaBitRateKb
}
function Grant-CsTeamsMeetingPolicy {
    param($Identity, $PolicyName)
    $global:writes++
    $global:meetingAssignment = $PolicyName
}
function Reset-Fakes {
    $global:writes = 0; $global:failAssignment = $false
    $global:appAssignment = 'previous'; $global:meetingAssignment = 'Global'
    $global:apps = @{ previous = [pscustomobject]@{ Identity = 'Tag:previous'; AppIds = @('previous-app'); Description = 'another policy' } }
    $global:meetings = @{ Global = [pscustomobject]@{ Identity = 'Global'; Description = 'original'; AllowTranscription = $false; AllowCloudRecording = $false; AllowIPVideo = $true; MediaBitRateKb = 1500 } }
}
Reset-Fakes
$preview = & $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer -EnableTranscription -Preview
Assert-True ($global:writes -eq 0) 'Preview changed Teams'
Assert-True ($preview.PreviousMeetingPolicy -eq 'Global' -and $preview.PreviousAllowTranscription -eq $false) 'Preview omitted rollback baseline'
& $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer -EnableTranscription -WhatIf | Out-Null
Assert-True ($global:writes -eq 0) 'WhatIf changed Teams'
foreach ($existing in @($false, $true)) {
    Reset-Fakes
    if ($existing) { $global:apps['sample-organizer'] = [pscustomobject]@{ Identity = 'Tag:sample-organizer'; AppIds = @('dedicated-existing'); Description = $owner } }
    & $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer -EnableTranscription | Out-Null
    $result = $global:apps['sample-organizer']
    Assert-True ('previous-app' -in $result.AppIds -and $app -in $result.AppIds) 'Permitted applications lost'
    if ($existing) { Assert-True ('dedicated-existing' -in $result.AppIds) 'Dedicated app lost' }
    $meeting = $global:meetings['sample-organizer-transcription']
    Assert-True ($meeting.AllowTranscription -eq $true -and $meeting.AllowCloudRecording -eq $false -and $meeting.AllowIPVideo -eq $true -and $meeting.MediaBitRateKb -eq 1500) 'Other meeting settings were overwritten'
    Assert-True ($global:meetings['Global'].AllowTranscription -eq $false) 'Changed Global transcription policy'
    Assert-True ($global:apps['previous'].AppIds[0] -eq 'previous-app') 'Changed previous application policy'
    & $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer -EnableTranscription | Out-Null
}
Reset-Fakes
& $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer | Out-Null
Assert-True ($global:meetingAssignment -eq 'Global' -and $global:meetings.Count -eq 1) 'Enabled transcription without explicit option'
Reset-Fakes
$global:failAssignment = $true
$failed = $false
try { & $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer | Out-Null }
catch { $failed = $_.Exception.Message -eq 'Effective organizer policy does not match the granted policy' }
Assert-True $failed 'Failed assignment reported success'
Reset-Fakes
$global:apps['sample-organizer'] = [pscustomobject]@{ Identity = 'Tag:sample-organizer'; AppIds = @('unrelated'); Description = 'unrelated shared policy' }
$failed = $false
try { & $scriptPath -TranscriptAppClientId $app -OrganizerObjectId $organizer -PolicyName sample-organizer | Out-Null } catch { $failed = $true }
Assert-True ($failed -and $global:writes -eq 0) 'Overwrote a policy with a different owner'
Write-Output 'Preview, WhatIf, application preservation, transcription cloning, idempotency and failed-assignment checks passed (offline).'

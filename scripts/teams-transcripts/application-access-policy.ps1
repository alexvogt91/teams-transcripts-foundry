# Use an authenticated Teams Administrator session (Connect-MicrosoftTeams).
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][Guid]$TranscriptAppClientId,
    [Parameter(Mandatory = $true)][Guid]$OrganizerObjectId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$')][string]$PolicyName,
    [switch]$Preview,
    [switch]$EnableTranscription
)
$ErrorActionPreference = 'Stop'
$owner = "teams-transcripts organizer $OrganizerObjectId app $TranscriptAppClientId"
$assignment = Get-CsUserPolicyAssignment -Identity $OrganizerObjectId -PolicyType ApplicationAccessPolicy
$effectiveName = $assignment.PolicyName
$effective = if ($effectiveName) { Get-CsApplicationAccessPolicy -Identity $effectiveName } else { $null }
$dedicated = Get-CsApplicationAccessPolicy | Where-Object { $_.Identity -eq "Tag:$PolicyName" }
if ($dedicated -and $dedicated.Description -ne $owner) { throw 'Requested application policy is not owned by this organizer/app helper; choose a fresh name' }
$appIds = @(@($effective.AppIds) + @($dedicated.AppIds) + @($TranscriptAppClientId.ToString()) | Where-Object { $_ } | Sort-Object -Unique)

$meetingAssignment = Get-CsUserPolicyAssignment -Identity $OrganizerObjectId -PolicyType TeamsMeetingPolicy
$meetingName = if ($meetingAssignment.PolicyName) { $meetingAssignment.PolicyName } else { 'Global' }
$baseline = Get-CsTeamsMeetingPolicy -Identity $meetingName
if ($null -eq $baseline) { throw 'Effective organizer transcription policy could not be read' }
$transcriptionName = "$PolicyName-transcription"
$copy = @{}
if ($EnableTranscription) {
    $parameters = (Get-Command New-CsTeamsMeetingPolicy).Parameters.Keys
    foreach ($property in $baseline.PSObject.Properties) {
        if ($property.Name -in $parameters -and $property.Name -notin @('Identity', 'Description', 'AllowTranscription') -and $null -ne $property.Value) {
            $copy[$property.Name] = $property.Value
        }
    }
    $existingMeeting = Get-CsTeamsMeetingPolicy | Where-Object { $_.Identity -eq "Tag:$transcriptionName" }
    if ($existingMeeting -and $existingMeeting.Description -ne $owner) { throw 'Requested transcription policy already exists with a different owner; choose a fresh name' }
}
$plan = [pscustomobject]@{
    OrganizerObjectId = $OrganizerObjectId.ToString()
    PreviousApplicationPolicy = $effectiveName
    ApplicationPolicy = $PolicyName
    PermittedApplications = $appIds
    PreviousMeetingPolicy = $meetingName
    PreviousAllowTranscription = $baseline.AllowTranscription
    EnableTranscription = $EnableTranscription.IsPresent
    TranscriptionPolicy = if ($EnableTranscription) { $transcriptionName } else { $null }
    PreservedMeetingSettings = $copy
}
if ($Preview) { return $plan }
if (-not $PSCmdlet.ShouldProcess($OrganizerObjectId, 'Assign dedicated application policy and optional transcription policy')) { return $plan }
if ($dedicated) {
    Set-CsApplicationAccessPolicy -Identity $PolicyName -AppIds $appIds
} else {
    New-CsApplicationAccessPolicy -Identity $PolicyName -AppIds $appIds -Description $owner | Out-Null
}
Grant-CsApplicationAccessPolicy -Identity $OrganizerObjectId -PolicyName $PolicyName
$effectiveAssignment = Get-CsUserPolicyAssignment -Identity $OrganizerObjectId -PolicyType ApplicationAccessPolicy
if (($effectiveAssignment.PolicyName -replace '^Tag:', '') -ne $PolicyName) { throw 'Effective organizer policy does not match the granted policy' }
$readBack = Get-CsApplicationAccessPolicy -Identity $effectiveAssignment.PolicyName
if (@($appIds | Where-Object { $_ -notin $readBack.AppIds }).Count -ne 0) { throw 'Effective policy omitted a required application' }
if ($EnableTranscription) {
    if ($existingMeeting) {
        Set-CsTeamsMeetingPolicy -Identity $transcriptionName -AllowTranscription $true @copy
    } else {
        New-CsTeamsMeetingPolicy -Identity $transcriptionName -Description $owner -AllowTranscription $true @copy | Out-Null
    }
    Grant-CsTeamsMeetingPolicy -Identity $OrganizerObjectId -PolicyName $transcriptionName
    $readAssignment = Get-CsUserPolicyAssignment -Identity $OrganizerObjectId -PolicyType TeamsMeetingPolicy
    if (($readAssignment.PolicyName -replace '^Tag:', '') -ne $transcriptionName) { throw 'Effective transcription assignment differs' }
    $readMeeting = Get-CsTeamsMeetingPolicy -Identity $readAssignment.PolicyName
    if ($readMeeting.AllowTranscription -ne $true) { throw 'Transcription was not enabled' }
    foreach ($name in $copy.Keys) {
        if (($readMeeting.$name | ConvertTo-Json -Depth 20 -Compress) -ne ($copy[$name] | ConvertTo-Json -Depth 20 -Compress)) { throw "Preserved meeting setting differs: $name" }
    }
}
Write-Output $plan
Write-Information 'Organizer policies verified; allow up to 30 minutes for Graph propagation.' -InformationAction Continue

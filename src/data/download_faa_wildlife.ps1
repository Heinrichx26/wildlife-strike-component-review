param(
    [int[]]$Years = @(2023, 2024, 2025, 2026),
    [switch]$Force,
    [int]$TimeoutSec = 240,
    [int]$MaxRetries = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..\..")
$rawDir = Join-Path $projectRoot "data\raw\faa_wildlife"
$metadataDir = Join-Path $projectRoot "data\metadata"
New-Item -ItemType Directory -Force -Path $rawDir, $metadataDir | Out-Null

$api = "https://wildlife.faa.gov/WildlifeAdmin/api/Service/exportPublicDatabase/"
$headers = @{
    "Content-Type" = "application/json"
    "Accept" = "application/json, text/plain, */*"
    "token" = "2120CCED-5527-4DEC-8B18-CC0DA7C3F6B2"
    "clientId" = "Website-FAA"
}

$inventory = New-Object System.Collections.Generic.List[object]

foreach ($year in $Years) {
    $outFile = Join-Path $rawDir ("faa_wildlife_export_{0}.json" -f $year)
    if ((Test-Path -LiteralPath $outFile) -and -not $Force) {
        $content = Get-Content -LiteralPath $outFile -Raw
        $json = $content | ConvertFrom-Json
        $rows = @($json.Result)
        $inventory.Add([pscustomobject]@{
            year = $year
            status = "existing"
            rows = $rows.Count
            bytes = (Get-Item -LiteralPath $outFile).Length
            file = $outFile
            error = ""
        })
        continue
    }

    $body = @{
        page = 1
        pageSize = 5
        sortBy = ""
        isSortAscending = $false
        selectedItemsProcessingSatus = @("")
        airportId = ""
        strikeReportTypeId = 1
        siIdentifiedTypeId = ""
        nonIndigenous = $false
        stateId = 0
        role = "PUBLIC"
        processingStatusId = "3"
        IncidentDateFrom = ("{0}-01-01T00:00:00.000Z" -f $year)
        IncidentDateTo = ("{0}-12-31T23:59:59.999Z" -f $year)
        LupdateDateFrom = "1990-01-01T00:00:00.000Z"
        LupdateDateTo = "2030-12-31T23:59:59.999Z"
    } | ConvertTo-Json -Depth 10 -Compress

    $lastError = $null
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $api -Method Post -Headers $headers -Body $body -UseBasicParsing -TimeoutSec $TimeoutSec
            $response.Content | Set-Content -LiteralPath $outFile -Encoding UTF8
            $json = $response.Content | ConvertFrom-Json
            $rows = @($json.Result)

            $inventory.Add([pscustomobject]@{
                year = $year
                status = "downloaded"
                rows = $rows.Count
                bytes = (Get-Item -LiteralPath $outFile).Length
                file = $outFile
                error = ""
            })
            $lastError = $null
            break
        } catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Seconds ([Math]::Min(30, 5 * $attempt))
        }
    }

    if ($lastError) {
        $inventory.Add([pscustomobject]@{
            year = $year
            status = "failed"
            rows = 0
            bytes = 0
            file = $outFile
            error = $lastError
        })
    }
}

$inventoryPath = Join-Path $metadataDir "faa_wildlife_download_inventory.csv"
$inventory | Export-Csv -LiteralPath $inventoryPath -NoTypeInformation -Encoding UTF8
$inventory | Format-Table -AutoSize
Write-Host ("Inventory saved: {0}" -f $inventoryPath)

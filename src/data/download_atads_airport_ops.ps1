param(
    [int]$StartYear = 1990,
    [int]$EndYear = 2025,
    [string]$Airport = "",
    [double]$SleepSeconds = 0.2,
    [int]$TimeoutSeconds = 300,
    [switch]$SkipExisting
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$RawDir = Join-Path $ProjectRoot "data\raw\atads\html"
$LogDir = Join-Path $ProjectRoot "data\metadata\download_logs"
New-Item -ItemType Directory -Force -Path $RawDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$FormUrl = "https://www.aspm.faa.gov/opsnet/sys/airport.asp"
$ServerUrl = "https://www.aspm.faa.gov/opsnet/sys/opsnet-server-x.asp"

$CalcFields = @(
    "SUM(IFR_ITIN_AC) AS IFR_ITIN_AC",
    "SUM(IFR_ITIN_AT) AS IFR_ITIN_AT",
    "SUM(IFR_ITIN_GA) AS IFR_ITIN_GA",
    "SUM(IFR_ITIN_MI) AS IFR_ITIN_MI",
    "SUM(IFR_ITIN_AC+IFR_ITIN_AT+IFR_ITIN_GA+IFR_ITIN_MI) AS TOT_ITII",
    "SUM(VFR_ITIN_AC) AS VFR_ITIN_AC",
    "SUM(VFR_ITIN_AT) AS VFR_ITIN_AT",
    "SUM(VFR_ITIN_GA) AS VFR_ITIN_GA",
    "SUM(VFR_ITIN_MI) AS VFR_ITIN_MI",
    "SUM(VFR_ITIN_AC+VFR_ITIN_AT+VFR_ITIN_GA+VFR_ITIN_MI) AS TOT_ITIV",
    "SUM(AC) AS AC",
    "SUM(ATAXI) AS ATAXI",
    "SUM(IFR_ITIN_GA+VFR_ITIN_GA) AS GA",
    "SUM(IFR_ITIN_MI+VFR_ITIN_MI) AS MIL",
    "SUM(AC+ATAXI+IFR_ITIN_GA+VFR_ITIN_GA+IFR_ITIN_MI+VFR_ITIN_MI) AS TOT_ITI",
    "SUM(LOCAL_GA) AS LOCAL_GA",
    "SUM(LOCAL_MIL) AS LOCAL_MIL",
    "SUM(LOCAL_GA+LOCAL_MIL) AS TOT_LOC",
    "SUM(TOTAL) AS TOTAL"
) -join ","

function New-AtadsBody {
    param([int]$Year, [string]$AirportCode)
    $Start = "{0}01" -f $Year
    $End = "{0}12" -f $Year
    $Where = "YYYYMM>=$Start AND YYYYMM<=$End"
    $LList = ""
    if ($AirportCode.Trim().Length -gt 0) {
        $Codes = $AirportCode -split "[,\s]+" | Where-Object { $_.Trim().Length -gt 0 } | ForEach-Object { $_.Trim().ToUpperInvariant() }
        $LList = ($Codes | ForEach-Object { "'$_'" }) -join ","
        $Where = "$Where AND LOCID IN ($LList)"
    }
    $Line = "SELECT LOCID,YYYYMM,$CalcFields FROM TOWER_DAY WHERE $Where GROUP BY LOCID,YYYYMM ORDER BY LOCID,YYYYMM"
    return @{
        dstyle = "m"
        dfld = "yyyymm"
        dlist = ""
        fromdate = $Start
        todate = $End
        llist = $LList
        keylist = "LOCID,YYYYMM"
        line = $Line
        cmd = "air_bas"
        nopage = "y"
        nost = "y"
        defs = ""
        avgdays = "1"
        oktosave = "y"
        addifr = ""
        addvfr = ""
        additi = "y"
        addloc = "y"
        reptype = "bas"
        reportformat = "asp"
        facilityType = "l"
        ftype = "0"
        iti = "1"
        loc = "1"
    }
}

$Session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest -Uri $FormUrl -WebSession $Session -UseBasicParsing -TimeoutSec $TimeoutSeconds | Out-Null

$Log = New-Object System.Collections.Generic.List[object]
foreach ($Year in $StartYear..$EndYear) {
    $Started = Get-Date
    $Status = "ok"
    $OutName = if ($Airport.Trim().Length -gt 0) {
        $Codes = $Airport -split "[,\s]+" | Where-Object { $_.Trim().Length -gt 0 }
        $Label = if ($Codes.Count -eq 1) { $Codes[0].Trim().ToUpperInvariant() } else { "SELECTED" }
        "atads_airport_ops_${Year}_${Label}.html"
    } else {
        "atads_airport_ops_${Year}.html"
    }
    $OutPath = Join-Path $RawDir $OutName
    try {
        if ($SkipExisting -and (Test-Path -LiteralPath $OutPath) -and ((Get-Item -LiteralPath $OutPath).Length -gt 100000)) {
            $Status = "skipped_existing"
            Write-Host "${Year}: skipped existing -> $OutName"
        } else {
            $Body = New-AtadsBody -Year $Year -AirportCode $Airport
            $Response = Invoke-WebRequest -Uri $ServerUrl -Method Post -Body $Body -WebSession $Session -UseBasicParsing -Headers @{ Referer = $FormUrl } -TimeoutSec $TimeoutSeconds
            Set-Content -Path $OutPath -Value $Response.Content -Encoding UTF8
            Write-Host "${Year}: ok -> $OutName"
        }
    } catch {
        $Status = "error: $($_.Exception.Message)"
        Write-Host "${Year}: $Status"
    }
    $Elapsed = ((Get-Date) - $Started).TotalSeconds
    $Log.Add([pscustomobject]@{
        year = $Year
        airport = if ($Airport.Trim().Length -gt 0) { $Airport.Trim().ToUpperInvariant() } else { "ALL" }
        status = $Status
        seconds = [math]::Round($Elapsed, 2)
        output = $OutName
    })
    Start-Sleep -Seconds $SleepSeconds
}

$LogPath = Join-Path $LogDir "atads_airport_ops_html_download_log_${StartYear}_${EndYear}.csv"
$Log | Export-Csv -Path $LogPath -NoTypeInformation -Encoding UTF8
Write-Host "wrote $LogPath"

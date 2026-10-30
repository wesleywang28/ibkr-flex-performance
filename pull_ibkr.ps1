<#
  IBKR Flex Web Service pull script

  First run:  asks for your Query ID and Token. The token is encrypted with Windows DPAPI,
              so only the current Windows user on this computer can decrypt it.
  Later runs: downloads the latest Flex report into data\raw\, updates data\latest.xml,
              downloads SPY/QQQ benchmark prices, and - if Python is installed -
              rebuilds ibkr_dashboard.html.

  Parameters:
    -Reset      enter a new Query ID / Token (e.g. after the token expires or is regenerated)
    -Scheduled  used by the scheduled task (never prompts; shows a Windows notification on failure)
#>
param(
    [switch]$Reset,
    [switch]$Scheduled
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root     = $PSScriptRoot
$CfgDir   = Join-Path $Root 'config'
$CfgFile  = Join-Path $CfgDir 'flex_credentials.xml'
$DataDir  = Join-Path $Root 'data'
$RawDir   = Join-Path $DataDir 'raw'
$HistDir  = Join-Path $DataDir 'history'
$Latest   = Join-Path $DataDir 'latest.xml'
$LogFile  = Join-Path $DataDir 'pull_log.txt'
$BaseUrl  = 'https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService'
$UA       = 'IBKR-Flex-Pull/1.0 (PowerShell)'
$KeepRecent = 14   # keep the N most recent pulls; older ones are thinned to one per month

foreach ($d in @($CfgDir, $DataDir, $RawDir, $HistDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

function Write-Log([string]$msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# Show a Windows notification when a scheduled (background) run fails
function Show-Notice([string]$title, [string]$msg) {
    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Warning
        $ni.BalloonTipIcon  = 'Warning'
        $ni.BalloonTipTitle = $title
        $ni.BalloonTipText  = if ($msg.Length -gt 240) { $msg.Substring(0, 240) } else { $msg }
        $ni.Visible = $true
        $ni.ShowBalloonTip(20000)
        Start-Sleep -Seconds 10
        $ni.Dispose()
    } catch { }
}

function Fail([string]$msg) {
    Write-Log "ERROR: $msg"
    if ($Scheduled) { Show-Notice 'IBKR data pull failed' "$msg`nSee data\pull_log.txt, or run run_pull.bat to retry." }
    exit 1
}

# ---------- 1. Load / save credentials ----------
if ($Reset -or -not (Test-Path $CfgFile)) {
    if ($Scheduled) { Fail 'No token saved yet. Run run_pull.bat once to set it up.' }
    Write-Host ''
    Write-Host '=== First-time setup: IBKR Flex Web Service ===' -ForegroundColor Cyan
    Write-Host '(The token is not echoed while you type or paste it; just press Enter.)'
    $qid = (Read-Host 'Query ID').Trim()
    $tok = Read-Host 'Token' -AsSecureString
    if (-not $qid) { Write-Host 'Query ID cannot be empty.' -ForegroundColor Red; exit 1 }
    [pscustomobject]@{ QueryId = $qid; Token = $tok } | Export-Clixml -Path $CfgFile
    Write-Host 'Saved (encrypted).' -ForegroundColor Green
}

$cfg     = Import-Clixml -Path $CfgFile
$QueryId = [string]$cfg.QueryId
$Token   = ([System.Net.NetworkCredential]::new('', $cfg.Token).Password).Trim()

# ---------- 2. HTTP helper (UTF-8 decoding + retries on network errors) ----------
function Invoke-Flex([string]$url) {
    for ($i = 1; $i -le 3; $i++) {
        try {
            $resp  = Invoke-WebRequest -Uri $url -UserAgent $UA -UseBasicParsing -TimeoutSec 180
            $bytes = $resp.RawContentStream.ToArray()
            return [Text.Encoding]::UTF8.GetString($bytes)
        } catch {
            if ($i -eq 3) { throw }
            Write-Log "Network problem, retrying in $(10 * $i) s: $($_.Exception.Message)"
            Start-Sleep -Seconds (10 * $i)
        }
    }
}

# Error codes worth waiting for: 1009 server busy / 1018 too many requests / 1019 generating / 1021 temporarily unavailable
$RetryCodes = @('1009', '1018', '1019', '1021')

try {
    # ---------- 3. Request the report ----------
    Write-Log "Starting pull (Query ID $QueryId)"
    $sendBody = Invoke-Flex "${BaseUrl}/SendRequest?t=${Token}&q=${QueryId}&v=3"
    $send = ([xml]$sendBody).FlexStatementResponse
    if ($send.Status -ne 'Success') {
        $m = "Request failed: [$($send.ErrorCode)] $($send.ErrorMessage)"
        if ($send.ErrorCode -in @('1012', '1015')) {
            $m += ' The token is invalid or expired: generate a new one in IBKR, then run reset_token.bat.'
        }
        Fail $m
    }
    $ref     = [string]$send.ReferenceCode
    $getBase = if ($send.Url) { [string]$send.Url } else { "${BaseUrl}/GetStatement" }

    # ---------- 4. Poll until the report is ready ----------
    $content = $null
    for ($n = 1; $n -le 20; $n++) {
        Start-Sleep -Seconds ([Math]::Min(5 * $n, 30))
        $body = Invoke-Flex "${getBase}?t=${Token}&q=${ref}&v=3"
        if ($body -match '<FlexQueryResponse') { $content = $body; break }

        $code = ''; $msg = ''
        try {
            $r = ([xml]$body).FlexStatementResponse
            $code = [string]$r.ErrorCode; $msg = [string]$r.ErrorMessage
        } catch {
            $msg = $body.Substring(0, [Math]::Min(200, $body.Length))
        }
        if ($code -in $RetryCodes) { Write-Log "Report is being generated [$code], waiting (attempt $n)..."; continue }
        Fail "Download failed: [$code] $msg"
    }
    if (-not $content) { Fail 'Timed out waiting for the report. Try again in a few minutes.' }

    # ---------- 5. Validate and save ----------
    $doc  = [xml]$content
    # With "Breakout by Day" each trading day is its own FlexStatement; take the earliest / latest dates
    $stmts = @($doc.FlexQueryResponse.FlexStatements.FlexStatement)
    if ($stmts.Count -eq 0) { Fail 'The report contains no FlexStatement. Check the query configuration.' }

    $fromDate = ($stmts | ForEach-Object { [string]$_.fromDate } | Sort-Object | Select-Object -First 1)
    $toDate   = ($stmts | ForEach-Object { [string]$_.toDate }   | Sort-Object | Select-Object -Last 1)
    $stamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
    $out    = Join-Path $RawDir ("flex_{0}_{1}.xml" -f $toDate, $stamp)
    $utf8   = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllText($out, $content, $utf8)
    [IO.File]::WriteAllText($Latest, $content, $utf8)

    $cnt = [ordered]@{
        'days'      = $stmts.Count
        'nav_rows'  = $doc.SelectNodes('//EquitySummaryByReportDateInBase').Count
        'cash_txns' = $doc.SelectNodes('//CashTransaction').Count
        'trades'    = $doc.SelectNodes('//Trade').Count
        'positions' = $doc.SelectNodes('//OpenPosition').Count
    }
    $summary = ($cnt.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', '
    Write-Log "Success: $fromDate ~ $toDate | $summary"
    Write-Log "Saved: $out"
    if ($cnt['days'] -le 2) {
        Write-Log 'Note: only 1-2 days returned. Make sure "Breakout by Day" is set to Yes in the Flex Query.'
    }

    # ---------- 6. Benchmarks (SPY/QQQ daily adjusted closes from Yahoo); failures don't affect account data ----------
    $BenchDir = Join-Path $DataDir 'benchmarks'
    if (-not (Test-Path $BenchDir)) { New-Item -ItemType Directory -Path $BenchDir -Force | Out-Null }
    foreach ($sym in @('SPY', 'QQQ')) {
        $ok = $false
        foreach ($h in @('query1', 'query2')) {
            $u = "https://${h}.finance.yahoo.com/v8/finance/chart/${sym}?range=max&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
            try {
                $resp = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 60 `
                          -UserAgent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
                $txt = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
                if ($txt -match '"timestamp"') {
                    [IO.File]::WriteAllText((Join-Path $BenchDir "$sym.json"), $txt, $utf8)
                    $ok = $true; break
                }
            } catch { }
        }
        if ($ok) { Write-Log "Benchmark $sym updated" } else { Write-Log "Benchmark $sym download failed (account data is unaffected)" }
    }

    # ---------- 7. Clean up: keep the N most recent pulls + the earliest pull of each month ----------
    $files = Get-ChildItem $RawDir -Filter 'flex_*.xml' | Sort-Object Name -Descending
    $keep  = @{}
    $files | Select-Object -First $KeepRecent | ForEach-Object { $keep[$_.FullName] = $true }
    $files | Group-Object { $_.Name.Substring(5, 6) } | ForEach-Object {
        $first = $_.Group | Sort-Object Name | Select-Object -First 1
        $keep[$first.FullName] = $true
    }
    $files | Where-Object { -not $keep[$_.FullName] } | Remove-Item -Force

    # ---------- 8. Rebuild the dashboard if Python is installed (failures don't affect the data) ----------
    $Builder = Join-Path $Root 'analysis\build_dashboard.py'
    if (Test-Path $Builder) {
        $py = $null
        foreach ($c in @('py', 'python', 'python3')) {
            $cmd = Get-Command $c -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $py = $cmd.Source; break }
        }
        if ($py) {
            $env:PYTHONIOENCODING = 'utf-8'
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { & $py $Builder $Root *> $null; $rc = $LASTEXITCODE } catch { $rc = -1 }
            $ErrorActionPreference = $old
            if ($rc -eq 0) { Write-Log 'Dashboard updated: ibkr_dashboard.html' }
            else { Write-Log "Dashboard build failed (exit code $rc); run build_dashboard.bat to see the details" }
        } else {
            Write-Log 'Python not found; skipped the dashboard build (install Python 3 and requirements.txt to enable it)'
        }
    }
}
catch {
    Fail "Unexpected error: $($_.Exception.Message)"
}

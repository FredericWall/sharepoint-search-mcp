[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$ReconcileDeletions,
    [switch]$AllowLargeDeletion,
    [string]$AppName = "sharepoint-mcp-db-tunnel",
    [string]$DatabaseService = "sharepoint_mcp_db",
    [string]$Schema = "sharepoint_mcp",
    [int]$LocalPort = 63306,
    [string]$PythonPath = "",
    [string]$ExpectedApi = $env:CF_API,
    [string]$ExpectedOrg = $env:CF_ORG,
    [string]$ExpectedSpace = $env:CF_SPACE
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tunnelProcess = $null
$locationPushed = $false
$exitCode = 1
$managedVariables = @(
    "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE",
    "INDEX_USE_LIBPQ_ENV", "INDEX_BACKEND", "POSTGRES_SCHEMA", "PYTHONUTF8",
    "PYTHONUNBUFFERED"
)
$previousEnvironment = @{}
foreach ($name in $managedVariables) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

if ($ReconcileDeletions -and -not $Apply) {
    throw "-ReconcileDeletions requires -Apply"
}

function Get-PropertyValue($Object, [string[]]$Names) {
    foreach ($name in $Names) {
        if ($null -ne $Object.PSObject.Properties[$name]) {
            $value = $Object.$name
            if ($null -ne $value -and "$value" -ne "") { return $value }
        }
    }
    return $null
}

try {
    Push-Location $repositoryRoot
    $locationPushed = $true
    if (-not (Test-Path -LiteralPath "indexer/.env")) {
        throw "indexer/.env is missing. Copy indexer/.env.example and set the local paths first."
    }

    if (-not (Get-Command cf -ErrorAction SilentlyContinue)) {
        throw "Cloud Foundry CLI was not found."
    }
    $target = (& cf target 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Cloud Foundry is not logged in. Run cf login and target the intended org and space."
    }
    foreach ($expectedValue in @($ExpectedApi, $ExpectedOrg, $ExpectedSpace)) {
        if ($expectedValue -and $target -notmatch [regex]::Escape($expectedValue)) {
            throw "Wrong CF target. Expected target output to contain: $expectedValue"
        }
    }

    $sshStatus = (& cf ssh-enabled $AppName 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $sshStatus -notmatch "enabled") {
        throw "SSH is not enabled for $AppName."
    }

    $appGuid = (& cf app $AppName --guid 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $appGuid) { throw "Could not resolve app $AppName." }
    $appEnvironment = (& cf curl "/v3/apps/$appGuid/env" | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw "Could not read the app service binding." }

    $binding = $null
    $services = $appEnvironment.system_env_json.VCAP_SERVICES
    foreach ($serviceGroup in $services.PSObject.Properties) {
        foreach ($candidate in @($serviceGroup.Value)) {
            if ($candidate.name -eq $DatabaseService) { $binding = $candidate }
        }
    }
    if ($null -eq $binding) { throw "Service $DatabaseService is not bound to $AppName." }

    $credentials = $binding.credentials
    $uriValue = Get-PropertyValue $credentials @("uri", "url")
    $remoteUri = if ($uriValue) { [Uri]$uriValue } else { $null }
    $remoteHost = Get-PropertyValue $credentials @("hostname", "host")
    $remotePort = Get-PropertyValue $credentials @("port")
    $databaseName = Get-PropertyValue $credentials @("dbname", "database")
    $databaseUser = Get-PropertyValue $credentials @("username", "user")
    $databasePassword = Get-PropertyValue $credentials @("password")

    if ($null -ne $remoteUri) {
        if (-not $remoteHost) { $remoteHost = $remoteUri.Host }
        if (-not $remotePort) { $remotePort = $remoteUri.Port }
        if (-not $databaseName) {
            $databaseName = [Uri]::UnescapeDataString($remoteUri.AbsolutePath.TrimStart("/"))
        }
        $userInfo = $remoteUri.UserInfo -split ":", 2
        if (-not $databaseUser -and $userInfo.Count -ge 1) {
            $databaseUser = [Uri]::UnescapeDataString($userInfo[0])
        }
        if (-not $databasePassword -and $userInfo.Count -eq 2) {
            $databasePassword = [Uri]::UnescapeDataString($userInfo[1])
        }
    }
    if (-not $remoteHost -or -not $remotePort -or -not $databaseName -or
        -not $databaseUser -or -not $databasePassword) {
        throw "The PostgreSQL binding does not contain the expected connection fields."
    }

    $portProbe = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback, $LocalPort
    )
    try {
        $portProbe.Start()
    } catch {
        throw "Local port $LocalPort is already in use. Choose another value with -LocalPort."
    } finally {
        $portProbe.Stop()
    }

    $cfExecutable = (Get-Command cf).Source
    $forward = "{0}:{1}:{2}" -f $LocalPort, $remoteHost, $remotePort
    $tunnelProcess = Start-Process -FilePath $cfExecutable -ArgumentList @(
        "ssh", $AppName, "--skip-remote-execution", "-L", $forward
    ) -PassThru -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $connected = $false
    while ([DateTime]::UtcNow -lt $deadline -and -not $connected) {
        if ($tunnelProcess.HasExited) {
            throw "The CF SSH tunnel process exited before opening the local port."
        }
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $task = $client.ConnectAsync("127.0.0.1", $LocalPort)
            $connected = $task.Wait(500) -and $client.Connected
        } catch {
            $connected = $false
        } finally {
            $client.Dispose()
        }
        if (-not $connected) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $connected) {
        throw "The CF SSH tunnel did not open local port $LocalPort within 30 seconds."
    }

    $env:PGHOST = "127.0.0.1"
    $env:PGPORT = "$LocalPort"
    $env:PGUSER = "$databaseUser"
    $env:PGPASSWORD = "$databasePassword"
    $env:PGDATABASE = "$databaseName"
    $env:PGSSLMODE = "require"
    $env:INDEX_USE_LIBPQ_ENV = "1"
    $env:INDEX_BACKEND = "postgres"
    $env:POSTGRES_SCHEMA = $Schema
    $env:PYTHONUTF8 = "1"
    $env:PYTHONUNBUFFERED = "1"

    if (-not $PythonPath) {
        $venvPython = Join-Path $repositoryRoot ".venv/Scripts/python.exe"
        $localPython = Join-Path $env:LOCALAPPDATA "Python/bin/python.exe"
        if (Test-Path -LiteralPath $venvPython) {
            $PythonPath = $venvPython
        } elseif (Test-Path -LiteralPath $localPython) {
            $PythonPath = $localPython
        } else {
            $PythonPath = (Get-Command python.exe).Source
        }
    }

    $indexerArguments = @("-m", "indexer.indexer")
    if ($Apply) { $indexerArguments += "--apply" }
    if ($ReconcileDeletions) { $indexerArguments += "--reconcile-deletions" }
    if ($AllowLargeDeletion) { $indexerArguments += "--allow-large-deletion" }
    & $PythonPath @indexerArguments
    $exitCode = $LASTEXITCODE
} finally {
    foreach ($name in $managedVariables) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
    $databasePassword = $null
    $credentials = $null
    $appEnvironment = $null
    if ($null -ne $tunnelProcess -and -not $tunnelProcess.HasExited) {
        Stop-Process -Id $tunnelProcess.Id
    }
    if ($locationPushed) { Pop-Location }
}

exit $exitCode

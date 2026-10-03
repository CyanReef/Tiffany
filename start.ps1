# Windows entry point. All arguments are forwarded to the shared launcher.
$launcherArgs = @($args)
$launcherPath = Join-Path $PSScriptRoot "launcher.py"
$explicitPython = -not [string]::IsNullOrWhiteSpace($env:TIFFANY_PYTHON)
$candidates = @()
$probeFailures = @()

if ($explicitPython) {
    if (Test-Path -LiteralPath $env:TIFFANY_PYTHON -PathType Leaf) {
        $candidates += @{ Path = (Get-Item -LiteralPath $env:TIFFANY_PYTHON).FullName; Arguments = @() }
    } else {
        $command = Get-Command $env:TIFFANY_PYTHON -CommandType Application -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += @{ Path = $command.Source; Arguments = @() }
        } else {
            $probeFailures += "TIFFANY_PYTHON could not be resolved: $env:TIFFANY_PYTHON"
        }
    }
} else {
    # Use the same Python as the terminal first. Validate aliases and shims by executing them.
    foreach ($name in @("python", "py", "python3")) {
        $commands = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
        foreach ($command in $commands) {
            $prefix = @()
            if ($name -eq "py") { $prefix = @("-3") }
            $candidates += @{ Path = $command.Source; Arguments = $prefix }
        }
    }
}

$pythonPath = $null
$pythonArgs = @()
$probeCode = "import sys; sys.version_info >= (3, 11) or sys.exit('Python 3.11+ required, found ' + sys.version.split()[0]); import venv"
foreach ($candidate in $candidates) {
    $candidatePath = $candidate.Path
    $candidateArgs = $candidate.Arguments
    try {
        $probeOutput = @(& $candidatePath @candidateArgs -B -c $probeCode 2>&1)
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = $candidatePath
            $pythonArgs = $candidateArgs
            break
        }
        $detail = ($probeOutput | Out-String).Trim()
        $probeFailures += "${candidatePath}: exit code $LASTEXITCODE. $detail"
    } catch {
        $probeFailures += "${candidatePath}: $($_.Exception.Message)"
    }
}

if (-not $pythonPath) {
    [Console]::Error.WriteLine("Tiffany requires Python 3.11+ and venv. Install Python or set TIFFANY_PYTHON to its executable path.")
    if ($candidates.Count -eq 0 -and -not $explicitPython) {
        $probeFailures += "No python, py or python3 command was found on PATH."
    }
    foreach ($failure in $probeFailures) {
        [Console]::Error.WriteLine("  {0}", $failure)
    }
    exit 2
}

$previousUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = "1"
    & $pythonPath @pythonArgs -B $launcherPath @launcherArgs
    $launcherExitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("Unable to start Tiffany: {0}", $_.Exception.Message)
    $launcherExitCode = 2
} finally {
    $env:PYTHONUTF8 = $previousUtf8
}
exit $launcherExitCode

# Regression suite for the ABI-matched gateway launcher (PR #19108).
#
# WHY THIS SUITE EXISTS
# ---------------------
# PR #19108 added `Resolve-HermesLauncher` (hermes/scripts/resolve-hermes-launcher.ps1)
# and called it from gateway-supervisor.ps1 / gateway-restart-remote.ps1. The call
# was made at SCRIPT LOAD TIME. The resolver THROWS by design when the host has no
# ABI-compatible launcher (no silent fallback - PROCESS-KEY-PRINCIPLES.md section 4), and
# the Linux CI runner has no Windows Hermes home at all - so loading the supervisor
# there threw and killed the whole script. That broke two existing Pester suites
# (hermes-validation, run 43713301273, 2026-11-15):
#   gateway-supervisor-root-lane.Tests.ps1       -> "Error occurred in test script" (0 ms, 25 tests lost)
#   gateway-supervisor-elevation-guard.Tests.ps1 -> child exit 1 instead of 0 / 12
# It also dropped the hermes/tests total below the CI floor ($minTotal = 632), which
# is a HARD gate: `if ($total -lt $minTotal) { $stepExit = 1 }`.
#
# WHAT THIS SUITE PINS
# ---------------------
#   1. Resolve-HermesLauncher still fails CLOSED - no candidate, no ABI-compatible
#      interpreter - and the ABI probe itself rejects an unusable candidate.
#   2. The callers LOAD without throwing (the documented side-effect-free TEST SEAM,
#      t_7973d4d5) and expose a lazy accessor that fails closed when it is USED.
#   3. Neither caller resolves the launcher at load time any more - a static guard
#      against re-introducing the bug that broke CI.

$Script:HeremesDir  = Split-Path -Parent $PSScriptRoot
$Script:ScriptsDir = Join-Path $Script:HeremesDir 'scripts'
$Script:Resolver   = Join-Path $Script:ScriptsDir 'resolve-hermes-launcher.ps1'
$Script:Supervisor = Join-Path $Script:ScriptsDir 'gateway-supervisor.ps1'
$Script:Remote     = Join-Path $Script:ScriptsDir 'gateway-restart-remote.ps1'

# This test file's own path. Captured at file scope because $PSCommandPath is
# not reliably populated inside a Pester v4 It scriptblock.
$Script:ThisFile = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }

# Load the supervisor the way the other suites do (dot-source), but capture the
# failure instead of letting it abort the whole file: a load-time throw must show
# up as a FAILED assertion with a readable message, not as a suite that silently
# contributes 0 tests (the exact shape that hid this bug).
$Script:SupervisorLoadError = $null
$Script:SavedErrorActionPreference = $ErrorActionPreference
try {
    . $Script:Supervisor
} catch {
    $Script:SupervisorLoadError = $_
}
$ErrorActionPreference = $Script:SavedErrorActionPreference

# t_bbc0d14b: the sweep now consults the CROSS-CYCLE SPAWN BACKOFF state. Dot-sourcing the real
# supervisor binds $HermesHome to the live host, so without this redirect the fake probe lanes in
# this suite would be written into the operator's real backoff state file.
$script:SpawnBackoffStatePathOverride = Join-Path ([System.IO.Path]::GetTempPath()) ("gw-backoff-test-" + [guid]::NewGuid().ToString('N') + ".json")

function New-EmptyHermesHome {
    $p = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-home-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    return $p
}

Describe "resolve-hermes-launcher.ps1 (ABI-matched launcher)" {

    Context "fails closed - never returns a launcher it cannot prove" {

        It "throws when no hermes.exe candidate exists under the home" {
            . $Script:Resolver
            $msg = $null
            try { Resolve-HermesLauncher -HermesHome (New-EmptyHermesHome) } catch { $msg = $_.Exception.Message }
            $msg | Should Not BeNullOrEmpty
            $msg | Should Match "no hermes\.exe candidate"
        }

        It "rejects a candidate whose interpreter is missing" {
            . $Script:Resolver
            $missing = Join-Path ([System.IO.Path]::GetTempPath()) "definitely-not-a-python-interpreter.exe"
            $probe = Test-HermesLauncherAbi -Python $missing -SitePackages ([System.IO.Path]::GetTempPath())
            $probe.Ok | Should BeFalse
            $probe.Reason | Should Match "interpreter missing"
        }

        It "rejects a candidate whose injected site-packages are missing" {
            . $Script:Resolver
            # Test-HermesLauncherAbi checks (1) the interpreter path exists,
            # (2) the site-packages path exists, and only THEN executes the
            # interpreter. The site-packages check therefore short-circuits
            # before python is ever run, so the "interpreter" only has to be an
            # existing path - it does NOT have to be a working python.
            #
            # This file's own path is used instead of `Get-Command python`:
            # the Linux CI runner has no `python` on PATH, and the previous
            # version bound an EMPTY string to the mandatory [string]$Python
            # parameter -> ParameterBindingValidationException, which failed
            # the test with a binding error instead of the expected assertion.
            $probe = Test-HermesLauncherAbi -Python $Script:ThisFile `
                                  -SitePackages (Join-Path ([System.IO.Path]::GetTempPath()) "no-such-site-packages-dir")
            $probe.Ok | Should BeFalse
            $probe.Reason | Should Match "site-packages missing"
        }
    }

    Context "the callers resolve the launcher LAZILY, never at load time" {

        It "gateway-supervisor.ps1 loads without throwing" {
            if ($Script:SupervisorLoadError) {
                throw ("gateway-supervisor.ps1 failed to load: " + $Script:SupervisorLoadError.Exception.Message)
            }
            $true | Should BeTrue
        }

        It "gateway-supervisor.ps1 exposes a lazy accessor that fails closed when used" {
            if ($Script:SupervisorLoadError) {
                throw ("gateway-supervisor.ps1 failed to load: " + $Script:SupervisorLoadError.Exception.Message)
            }
            $msg = $null
            try { Get-HermesExe -HermesHomeOverride (New-EmptyHermesHome) } catch { $msg = $_.Exception.Message }
            $msg | Should Not BeNullOrEmpty
            $msg | Should Match "no hermes\.exe candidate"
        }

        It "neither caller resolves the launcher at load time" {
            foreach ($f in @($Script:Supervisor, $Script:Remote)) {
                $src = Get-Content -LiteralPath $f -Raw
                $src | Should Not Match '(?m)^\$HermesExe\s*=\s*Resolve-HermesLauncher'
                $src | Should Match 'Get-HermesExe'
            }
        }

        It "every point-of-use launcher resolution goes through the non-throwing guard" {
            # t_b868309e item 1: a bare `$HermesExe = Get-HermesExe` under
            # $ErrorActionPreference = 'Stop' lets the resolver's deliberate throw
            # escape as a TERMINATING error and kill the supervisor. Both call sites
            # in the supervisor (Start-Gateway, Start-Serve) and the one in
            # gateway-restart-remote.ps1 must route through Resolve-HermesExeForLane,
            # which logs ERROR and returns $null instead.
            foreach ($f in @($Script:Supervisor, $Script:Remote)) {
                $src = Get-Content -LiteralPath $f -Raw
                $src | Should Not Match '(?m)^\s*\$HermesExe\s*=\s*Get-HermesExe\s*$'
                $src | Should Match 'function Resolve-HermesExeForLane'
                $src | Should Match '\$HermesExe\s*=\s*Resolve-HermesExeForLane'
            }
        }
    }

    # ---------------------------------------------------------------------
    # t_9fa8ab06 - the stage-2 bound is a BOUND, not a budget
    #
    # On the production host `import openai` costs 76-152 s (measured
    # 2026-11-24: 76332 / 92923 / 136071 / 151923 ms, with 1497 .pyc already
    # compiled), so the old 45 s client bound could NEVER return a verdict
    # there - it just turned 45.7 s of every supervisor boot into a WARN.
    # The bound was retuned to 10 s: the same verdict (WARN, candidate KEPT),
    # ~35 s of pre-supervision time back (measured 45701 -> 11047 ms).
    #
    # What must stay true after that retune, and what these cases pin:
    #   1. the constant is small enough to live on a pre-spawn path,
    #   2. the param doc states the constant that ships (it said 90 while the
    #      constant was 45 - the drift that hid this tuning question),
    #   3. the WARN quotes the bound that was ACTUALLY applied, so a caller's
    #      -ClientTimeoutSeconds is what the operator reads,
    #   4. a client import that FAILS is still rejected - that fast failure is
    #      the only thing the bound has to be larger than, so shrinking it
    #      must not turn a broken client package into an accepted candidate.
    # ---------------------------------------------------------------------
    Context "client-import bound (t_9fa8ab06 - one boot paid 45.7 s for no verdict)" {

        It "ships a client bound that can live on a pre-spawn path" {
            . $Script:Resolver
            $src = Get-Content -LiteralPath $Script:Resolver -Raw
            $clientMatch = [regex]::Match($src, '\$script:AbiClientTimeoutSeconds\s*=\s*(\d+)')
            $clientMatch.Success | Should BeTrue
            $canaryMatch = [regex]::Match($src, '\$script:AbiProbeTimeoutSeconds\s*=\s*(\d+)')
            $canaryMatch.Success | Should BeTrue

            # Bound in force, read from the file itself (not re-typed here).
            $script:AbiClientTimeoutSeconds | Should Be ([int]$clientMatch.Groups[1].Value)
            $script:AbiProbeTimeoutSeconds | Should Be ([int]$canaryMatch.Groups[1].Value)

            # 10 s: ~10x the fast-failure mode of a broken client package and an
            # order of magnitude below the 76 s minimum a "verdict" would need.
            $script:AbiClientTimeoutSeconds | Should Be 10
            # Ceiling guard: raising this back toward the old 45 s re-buys the
            # 43.5 s/boot dead cost the card exists to remove. 15 s leaves room
            # for a slower CI host without re-creating it.
            $script:AbiClientTimeoutSeconds | Should BeLessThan 15
            # The ABI verdict itself keeps its own, wider bound.
            $script:AbiProbeTimeoutSeconds | Should Be 60
        }

        It "documents the client-import default it actually ships" {
            . $Script:Resolver
            $src = Get-Content -LiteralPath $Script:Resolver -Raw
            $constant = [int]([regex]::Match($src, '\$script:AbiClientTimeoutSeconds\s*=\s*(\d+)')).Groups[1].Value
            $doc = [regex]::Match($src, 'Per-candidate client-import ceiling \(seconds\)\. 0 = script default \((\d+)\)')
            $doc.Success | Should BeTrue
            # The pre-fix doc said 90 while the constant was 45; a doc that can
            # disagree with the constant is where the next tuning question hides.
            [int]$doc.Groups[1].Value | Should Be $constant
        }

        It "reports the bound ACTUALLY applied, not the script default" {
            . $Script:Resolver
            $sp = Join-Path ([System.IO.Path]::GetTempPath()) ("abi-bound-sp-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $sp -Force | Out-Null

            # Stub the bounded-process helper (same name, same call shape) so the
            # stage-2 DECISION is exercised deterministically on any host: stage 1
            # answers, stage 2 overruns. No interpreter required -> identical on
            # the Linux runner and on Windows.
            function Invoke-HermesAbiProbeProcess {
                param(
                    [string]$FilePath,
                    [string]$Arguments,
                    [int]$TimeoutSeconds = 60,
                    [hashtable]$EnvironmentVariable = @{}
                )
                if ($Arguments -match 'pydantic_core') {
                    return [pscustomobject]@{ ExitCode = 0; StdOut = "3.14.9`n"; StdErr = ''; TimedOut = $false; DurationMs = 520; Error = $null }
                }
                return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true; DurationMs = ($TimeoutSeconds * 1000); Error = $null }
            }

            $probe = Test-HermesLauncherAbi -Python $Script:ThisFile -SitePackages $sp -ClientTimeoutSeconds 3
            $probe.Ok | Should BeTrue
            $probe.ClientUnverified | Should BeTrue
            # The applied bound travels with the verdict; the WARN reads THIS, so a
            # caller's -ClientTimeoutSeconds is what an operator is told.
            $probe.ClientBoundSeconds | Should Be 3

            # Source-shape pin: the WARN must format $probe.ClientBoundSeconds and
            # must NOT fall back to the script constant (the shape that reported 45
            # to a caller who had asked for 3).
            $resolverSrc = Get-Content -LiteralPath $Script:Resolver -Raw
            $resolverSrc | Should Match '\$probe\.ClientBoundSeconds'
            $resolverSrc | Should Not Match 'Raise -ClientTimeoutSeconds if this host is merely slow\.'
        }

        It "still REJECTS a client import that fails fast, despite the smaller bound" {
            . $Script:Resolver
            $sp = Join-Path ([System.IO.Path]::GetTempPath()) ("abi-bound-sp-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $sp -Force | Out-Null

            function Invoke-HermesAbiProbeProcess {
                param(
                    [string]$FilePath,
                    [string]$Arguments,
                    [int]$TimeoutSeconds = 60,
                    [hashtable]$EnvironmentVariable = @{}
                )
                if ($Arguments -match 'pydantic_core') {
                    return [pscustomobject]@{ ExitCode = 0; StdOut = "3.14.9`n"; StdErr = ''; TimedOut = $false; DurationMs = 520; Error = $null }
                }
                # A genuinely broken client package raises its ImportError in about a
                # second - far inside any sane bound - which is why shrinking the
                # bound cannot weaken this check.
                return [pscustomobject]@{
                    ExitCode = 1
                    StdOut   = ''
                    StdErr   = "ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`n"
                    TimedOut = $false
                    DurationMs = 900
                    Error    = $null
                }
            }

            $probe = Test-HermesLauncherAbi -Python $Script:ThisFile -SitePackages $sp -ClientTimeoutSeconds 10
            $probe.Ok | Should BeFalse
            $probe.Stage | Should Be 'client'
            $probe.ClientUnverified | Should BeFalse
            $probe.Reason | Should Match 'import probe failed'
            $probe.Reason | Should Match 'ArgsKwargs'
        }
    }
}

Describe "gateway-supervisor launcher robustness (t_b868309e)" {

    # The PowerShell host running THIS suite. Spawned by the probe cases so they do
    # not depend on a `pwsh` that may be absent from PATH on the runner.
    $Script:PwshExe = $null
    try { $Script:PwshExe = (Get-Process -Id $PID).Path } catch { $Script:PwshExe = $null }
    if (-not $Script:PwshExe -or -not (Test-Path -LiteralPath $Script:PwshExe)) {
        $cmd = Get-Command pwsh -ErrorAction SilentlyContinue
        if ($cmd) { $Script:PwshExe = $cmd.Source }
    }

    Context "probe bounding (item 2 - one cold probe cost 288882 ms)" {

        It "returns TimedOut instead of waiting forever when a probe hangs" {
            if (-not $Script:PwshExe) { throw 'cannot locate a PowerShell host to spawn the hanging probe with' }
            . $Script:Resolver
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $run = Invoke-HermesAbiProbeProcess -FilePath $Script:PwshExe `
                -Arguments '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 120"' `
                -TimeoutSeconds 2
            $sw.Stop()
            $run.TimedOut | Should BeTrue
            # The bound is what makes this test terminate at all: without it the wait
            # is 120 s, with it the child is killed at ~2 s.
            $sw.Elapsed.TotalSeconds | Should BeLessThan 60
            $run.DurationMs | Should BeLessThan 60000
            $run.Error | Should BeNullOrEmpty
        }

        It "does not leak the injected PYTHONPATH into the caller" {
            . $Script:Resolver
            $before = $env:PYTHONPATH
            $null = Invoke-HermesAbiProbeProcess -FilePath $Script:PwshExe `
                -Arguments '-NoProfile -NonInteractive -Command "exit 0"' `
                -TimeoutSeconds 30 `
                -EnvironmentVariable @{ PYTHONPATH = 'C:\injected\site-packages' }
            $env:PYTHONPATH | Should Be $before
        }

        It "rejects a candidate that cannot run the ABI canary, and reports its timing" {
            if (-not $Script:PwshExe) { throw 'cannot locate a PowerShell host to spawn the probe with' }
            . $Script:Resolver
            # A real, existing executable that is NOT a python interpreter: the canary
            # stage must REJECT it (exit code recorded) rather than hang or accept it.
            $sp = Join-Path ([System.IO.Path]::GetTempPath()) ("abi-probe-sp-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $sp -Force | Out-Null
            $probe = Test-HermesLauncherAbi -Python $Script:PwshExe -SitePackages $sp -TimeoutSeconds 30
            $probe.Ok | Should BeFalse
            $probe.Stage | Should Be 'canary'
            $probe.DurationMs | Should BeGreaterThan 0
            $probe.Reason | Should Match 'ABI canary'
        }
    }

    Context "a resolver throw must not kill the caretaker (item 1)" {

        It "a resolver throw inside Start-Gateway does not terminate the lane sweep" {
            if ($Script:SupervisorLoadError) {
                throw ("gateway-supervisor.ps1 failed to load: " + $Script:SupervisorLoadError.Exception.Message)
            }
            # Production shape from the card: the resolver throws (no ABI-compatible
            # launcher anywhere) and the throw reaches Start-Gateway from the sweep's
            # restart action. Dynamic scoping makes this shadowing REAL: PowerShell
            # resolves Get-HermesExe through the caller's scope chain, so the throw
            # lands inside the production Start-Gateway -> Resolve-HermesExeForLane
            # path (verified against this runtime, not assumed).
            function Get-HermesExe { throw 'simulated ABI resolver failure (t_b868309e)' }
            # Keep the LIVE supervisor log clean: Write-SupervisorLog reads
            # $SupervisorLogFile through the caller's scope chain, so a local value
            # here redirects the production log line into a per-test temp file - which
            # is also where the evidence for this case is asserted.
            $SupervisorLogFile = Join-Path ([System.IO.Path]::GetTempPath()) ("supervisor-guard-" + [guid]::NewGuid().ToString('N') + ".log")

            $lanes = @(
                [pscustomobject]@{ Name = 'probe-lane-a'; Port = 19901; Root = $false },
                [pscustomobject]@{ Name = 'probe-lane-b'; Port = 19902; Root = $false }
            )
            $logLines = New-Object System.Collections.ArrayList
            $reports = @(Invoke-SupervisorLaneSweep `
                -Lanes $lanes `
                -MaxAttemptsPerCycle 1 `
                -DownConfirmationsRequired 0 `
                -LivenessProbe { param($laneName) [pscustomobject]@{ Alive = $false; Reason = 'probe-dead'; PID = 0 } } `
                -RestartAction { param($laneName, $maxAttempts) $r = Start-Gateway -ProfileName $laneName; [void]$logLines.Add("start-gateway-returned:$laneName=$r"); return $r } `
                -LogWriter { param($message, $level) [void]$logLines.Add("$level|$message") } `
                -UpdatePauseMaxHoldSeconds 0)

            # The sweep RETURNED - the throw was contained, not propagated.
            $reports.Count | Should Be 2
            foreach ($r in $reports) {
                $r.RestartAttempted | Should BeTrue
                $r.Restarted | Should BeFalse
            }
            # Start-Gateway itself was reached and answered $false instead of throwing,
            # for BOTH lanes: the throw did not abort the lane behind it.
            @($logLines | Where-Object { $_ -eq 'start-gateway-returned:probe-lane-a=False' }).Count | Should Be 1
            @($logLines | Where-Object { $_ -eq 'start-gateway-returned:probe-lane-b=False' }).Count | Should Be 1
            # ... and the production guard logged the refusal at ERROR for each lane.
            $logText = if (Test-Path -LiteralPath $SupervisorLogFile) { Get-Content -LiteralPath $SupervisorLogFile -Raw } else { '' }
            $logText | Should Match 'REFUSE-START: Start-Gateway/probe-lane-a'
            $logText | Should Match 'REFUSE-START: Start-Gateway/probe-lane-b'
            $logText | Should Match 'simulated ABI resolver failure'
        }

        It "contains a throwing restart action instead of letting it kill the sweep" {
            if ($Script:SupervisorLoadError) {
                throw ("gateway-supervisor.ps1 failed to load: " + $Script:SupervisorLoadError.Exception.Message)
            }
            $lanes = @(
                [pscustomobject]@{ Name = 'probe-throw-lane'; Port = 19903; Root = $false }
            )
            $logLines = New-Object System.Collections.ArrayList
            $reports = @(Invoke-SupervisorLaneSweep `
                -Lanes $lanes `
                -MaxAttemptsPerCycle 1 `
                -DownConfirmationsRequired 0 `
                -LivenessProbe { param($laneName) [pscustomobject]@{ Alive = $false; Reason = 'probe-dead'; PID = 0 } } `
                -RestartAction { param($laneName, $maxAttempts) throw "restart action boom ($laneName)" } `
                -LogWriter { param($message, $level) [void]$logLines.Add("$level|$message") } `
                -UpdatePauseMaxHoldSeconds 0)

            $reports.Count | Should Be 1
            $reports[0].Restarted | Should BeFalse
            $reports[0].RestartError | Should Match 'restart action boom'
            @($logLines | Where-Object { $_ -match 'RESTART-ACTION THREW' }).Count | Should BeGreaterThan 0
        }
    }
}

# ---------------------------------------------------------------------------
# t_784ed637 (P0, 2026-11-26) - the launcher must be proved BEFORE the stop.
#
# Incident: gateway-restart-remote.ps1 -Profile default -Force -Inline stopped the
# live ROOT lane, and ONLY THEN did Start-Gateway -> Get-HermesExe run the ABI probe.
# The probe never returned (>7 m 50 s) so the lane stayed DOWN with no dispatcher -
# 22 ready cards, 0 spawns. Two independent defences are pinned here:
#   1. the preflight (Assert-LauncherResolvable) refuses while every lane is still UP;
#   2. the preflight is ORDERED before the first Stop-Gateway call, at both the run
#      level (whole wave) and the per-lane level (Restart-Gateway) - a source-order
#      assertion, because that ordering IS the property that prevents the outage.
# ---------------------------------------------------------------------------
Describe "gateway-restart-remote launcher preflight (t_784ed637)" {

    Context "Assert-LauncherResolvable refuses before anything is stopped" {

        It "names the lanes and says they are still UP when no launcher resolves" {
            $Script:PreflightMsg = $null
            & {
                . $Script:Remote
                # Stub the resolver at the seam the preflight uses; the child scope
                # discards the redefinition when the block ends.
                function Get-HermesExe { throw "stub: no ABI-compatible hermes.exe candidate" }
                try {
                    $null = Assert-LauncherResolvable -Names @('default', 'pm-bangioi')
                } catch {
                    $Script:PreflightMsg = $_.Exception.Message
                }
            }
            $Script:PreflightMsg | Should Not BeNullOrEmpty
            $Script:PreflightMsg | Should Match 'before any stop'
            $Script:PreflightMsg | Should Match 'still UP'
            $Script:PreflightMsg | Should Match 'default, pm-bangioi'
            $Script:PreflightMsg | Should Match 'stub: no ABI-compatible'
        }

        It "returns the resolved launcher when the resolution succeeds" {
            $Script:PreflightLauncher = $null
            & {
                . $Script:Remote
                function Get-HermesExe { 'C:\stub-home\installs\x\environments\y\Scripts\hermes.exe' }
                $Script:PreflightLauncher = Assert-LauncherResolvable -Names @('default')
            }
            $Script:PreflightLauncher | Should Be 'C:\stub-home\installs\x\environments\y\Scripts\hermes.exe'
        }

        It "treats an empty launcher as a refusal (never a silent pass)" {
            $Script:PreflightEmptyMsg = $null
            & {
                . $Script:Remote
                function Get-HermesExe { '' }
                try {
                    $null = Assert-LauncherResolvable -Names @('default')
                } catch {
                    $Script:PreflightEmptyMsg = $_.Exception.Message
                }
            }
            $Script:PreflightEmptyMsg | Should Match 'empty launcher'
            $Script:PreflightEmptyMsg | Should Match 'still UP'
        }
    }

    Context "the preflight is ORDERED before the first Stop-Gateway" {

        It "Restart-Gateway resolves the launcher before it stops the lane" {
            $src = Get-Content -LiteralPath $Script:Remote -Raw
            $start = $src.IndexOf('function Restart-Gateway {')
            $end = $src.IndexOf('# === MAIN ===', $start)
            $start | Should BeGreaterThan -1
            $end | Should BeGreaterThan $start
            $body = $src.Substring($start, $end - $start)

            $iPreflight = $body.IndexOf('Assert-LauncherResolvable -Names @($Name)')
            $iStop = $body.IndexOf('Stop-Gateway -Name $Name')
            $iPreflight | Should BeGreaterThan -1
            $iStop | Should BeGreaterThan -1
            $iPreflight | Should BeLessThan $iStop
        }

        It "MAIN resolves the launcher for the whole wave before the first Restart-Gateway call" {
            $src = Get-Content -LiteralPath $Script:Remote -Raw
            $main = $src.Substring($src.IndexOf('# === MAIN ==='))
            $iPreflight = $main.IndexOf('Assert-LauncherResolvable -Names $targets')
            $iFirstRestart = $main.IndexOf('Restart-Gateway -Name $name')
            $iPreflight | Should BeGreaterThan -1
            $iFirstRestart | Should BeGreaterThan -1
            $iPreflight | Should BeLessThan $iFirstRestart
        }

        It "Start-Gateway still resolves at its point of use (the cache, never a second probe)" {
            $src = Get-Content -LiteralPath $Script:Remote -Raw
            $start = $src.IndexOf('function Start-Gateway {')
            $body = $src.Substring($start, $src.IndexOf('function Wait-PortListening', $start) - $start)
            # The point-of-use call must go through the non-throwing guard AND read the
            # cache Get-HermesExe filled during the preflight.
            $body | Should Match 'Resolve-HermesExeForLane'
            $src | Should Match '(?s)function Get-HermesExe \{.*?\$script:HermesExeCache'
        }
    }
}

# ---------------------------------------------------------------------------
# t_784ed637 Defect 2 (P1) - a restart must CLAIM the lane before it stops it.
#
# Measured 2026-11-26 06:16-06:19: while the ROOT lane was down mid-restart, nine
# surviving kanban_worker_client processes replayed the lane argv within 40 s and
# fought over the home (one took gateway.lock and never published a host record,
# eight died on the duplicate-instance guard), and the lane stayed unbound for
# another 11 minutes.
#
# The claim is a self-expiring file the python spawn path honours
# (hermes_cli/gateway_runtime_start.py::_restart_lease). These cases pin the
# WRITER's contract - the schema python parses, the expiry it can trust, and the
# ordering that makes the claim useful (written before the stop, withdrawn after).
# ---------------------------------------------------------------------------
Describe "gateway-restart-remote restart lease (t_784ed637 defect 2)" {

    Context "the lease file is the schema python reads" {

        It "writes {pid, started_at, expires_at} as epoch seconds inside the cap" {
            $lane = Join-Path ([System.IO.Path]::GetTempPath()) ("t784-lease-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $lane -Force | Out-Null
            try {
                $Script:LeaseOk = $null; $Script:Lease = $null
                & {
                    . $Script:Remote
                    $Script:LeaseOk = Write-RestartLease -LaneHome $lane -Seconds 300 -Reason 'pester'
                    $Script:Lease = Get-Content -LiteralPath (Join-Path $lane '.gateway-restart-in-progress.json') -Raw | ConvertFrom-Json
                }
                $Script:LeaseOk | Should BeTrue
                $Script:Lease.pid | Should BeGreaterThan 0
                $Script:Lease.reason | Should Be 'pester'
                # epoch SECONDS: a millisecond stamp would be ~1000x past any cap python accepts.
                [int64]$Script:Lease.started_at | Should BeGreaterThan 1000000000
                [int64]$Script:Lease.expires_at | Should BeGreaterThan ([int64]$Script:Lease.started_at)
                ([int64]$Script:Lease.expires_at - [int64]$Script:Lease.started_at) | Should Be 300
            } finally {
                Remove-Item -LiteralPath $lane -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        It "clamps an over-long window to the cap python honours (never past 1800 s)" {
            $lane = Join-Path ([System.IO.Path]::GetTempPath()) ("t784-lease-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $lane -Force | Out-Null
            try {
                $Script:LeaseCapped = $null
                & {
                    . $Script:Remote
                    $null = Write-RestartLease -LaneHome $lane -Seconds 99999
                    $Script:LeaseCapped = Get-Content -LiteralPath (Join-Path $lane '.gateway-restart-in-progress.json') -Raw | ConvertFrom-Json
                }
                ([int64]$Script:LeaseCapped.expires_at - [int64]$Script:LeaseCapped.started_at) | Should Be 1800
            } finally {
                Remove-Item -LiteralPath $lane -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        It "never throws when the lane home is unwritable (the lease is not a safety property)" {
            $Script:LeaseMissingOk = $null
            & {
                . $Script:Remote
                $missing = Join-Path ([System.IO.Path]::GetTempPath()) ("t784-no-such-lane-" + [guid]::NewGuid().ToString('N'))
                $Script:LeaseMissingOk = Write-RestartLease -LaneHome $missing -Seconds 60
            }
            $Script:LeaseMissingOk | Should BeFalse
        }

        It "withdraws the lease" {
            $lane = Join-Path ([System.IO.Path]::GetTempPath()) ("t784-lease-" + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $lane -Force | Out-Null
            try {
                $path = Join-Path $lane '.gateway-restart-in-progress.json'
                & {
                    . $Script:Remote
                    $null = Write-RestartLease -LaneHome $lane -Seconds 60
                    (Test-Path -LiteralPath $path) | Should BeTrue
                    Remove-RestartLease -LaneHome $lane
                }
                (Test-Path -LiteralPath $path) | Should BeFalse
            } finally {
                Remove-Item -LiteralPath $lane -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Context "the claim is ORDERED before the stop and withdrawn after it" {

        It "Restart-Gateway claims the lane before it stops it, and withdraws in a finally" {
            $src = Get-Content -LiteralPath $Script:Remote -Raw
            $start = $src.IndexOf('function Restart-Gateway {')
            $end = $src.IndexOf('# === MAIN ===', $start)
            $body = $src.Substring($start, $end - $start)

            # DETACHED path: claim -> stop, and the claim survives the return (the deferred
            # child owns it, so nothing removes it here).
            $iDetachedWrite = $body.IndexOf("Write-RestartLease -LaneHome (Get-LaneHome -Name `$Name) -Reason 'restart-remote-detached'")
            $iFirstStop = $body.IndexOf('Stop-Gateway -Name $Name')
            $iDetachedWrite | Should BeGreaterThan -1
            $iFirstStop | Should BeGreaterThan -1
            $iDetachedWrite | Should BeLessThan $iFirstStop

            # INLINE path: measured inside its own branch, where claim -> stop -> finally{remove}.
            $inline = $body.Substring($body.IndexOf('# Legacy inline path'))
            $iWrite = $inline.IndexOf('Write-RestartLease -LaneHome $laneHome')
            $iStop = $inline.IndexOf('Stop-Gateway -Name $Name')
            $iFinally = $inline.LastIndexOf('} finally {')
            $iRemove = $inline.LastIndexOf('Remove-RestartLease -LaneHome $laneHome')
            $iWrite | Should BeGreaterThan -1
            $iStop | Should BeGreaterThan -1
            $iWrite | Should BeLessThan $iStop
            $iFinally | Should BeGreaterThan $iStop
            $iRemove | Should BeGreaterThan $iFinally
        }
    }
}

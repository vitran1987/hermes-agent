<#
.SYNOPSIS
    Remote gateway restart helper -- callable from Telegram or CLI.
.DESCRIPTION
    Safely restarts one or more Hermes gateways with verification.
    Designed to be called remotely (e.g., from Telegram bot commands)
    when the user is away from desktop.

    *** HARD RULE (t_d4283b6b, 2026-11-02) ***
    A kanban worker MUST NEVER invoke this script. This is PM/CEO runbook
    territory only. A worker that invokes this script will poison the
    gateway environment with its own delegation markers
    (HERMES_DELEGATED_CHILD_CONTEXT, HERMES_KANBAN_TASK, etc.), fencing
    the kanban dispatcher and killing all worker spawning. The
    Remove-WorkerEnvVars function below mitigates this, but the correct
    fix is: do not run this script from a worker context at all.

    Port assignment is derived from each profile's own config.yaml
    (``port:`` field under ``gateway:``) at runtime -- the previous hard-coded
    table drifted away from reality and silently killed the wrong gateway
    (e.g. ``-Profile pm-bangioi`` killed ``testlead-bangioi`` instead).

    After Start-Gateway the script polls the TCP listen state, not just the
    lock-file PID -- a brand-new pwsh.exe may have spawned but the embedded
    python child has not bound A2A_PORT yet, leaving the next iteration's
    Stop-Gateway racing the TIME_WAIT reaper on the previous port.

.PARAMETER Profile
    Which gateway to restart: all, pm-bangioi, tl-bangioi, testlead-bangioi,
    researchlead-bangioi, docwriter-bangioi, senior-techlead-bangioi,
    developer-bangioi, code-reviewer-bangioi, security-reviewer-bangioi,
    coo-bangioi, cpo-bangioi, cco-bangioi.
.PARAMETER Force
    Force kill without graceful shutdown.
.PARAMETER WaitVerify
    Wait and verify the gateway comes back up (default: true).
.PARAMETER MaxWaitSeconds
    Per-profile wait ceiling in seconds.
.PARAMETER Inline
    Restart inline in this process (stop, start, wait for the port to enter
    LISTEN, health check) instead of deferring the restart 1 minute through a
    schtasks task. The deferred task invokes this script with -Inline; without
    it the task would schedule another deferred restart instead of starting the
    gateway.
.PARAMETER CleanupTaskName
    Name of the deferred one-shot scheduled task to delete when this run
    finishes. The deferred task passes its OWN name so it removes itself
    instead of lingering forever (207 orphaned `hermes-restart-*` tasks piled
    up on the host before this). The deletion happens in a finally block, so
    it runs on the success path AND the failure path.
.EXAMPLE
    .\gateway-restart-remote.ps1 -Profile tl-bangioi
    .\gateway-restart-remote.ps1 -Profile all
    .\gateway-restart-remote.ps1 -Profile tl-bangioi -Force
.NOTES
    Item #3 of round-3 Hermes tooling fixes -- port-mapping drift + bind wait.
#>

param(
    [Parameter(Mandatory=$false)]
    # t_e4882d04: ValidateSet is a COMPILE-TIME literal and cannot reference
    # $KnownProfiles. When adding a profile you MUST edit BOTH this list AND
    # $KnownProfiles below — Assert-SupervisorProfileParity() fails CI if they drift.
    # t_7973d4d5: "default" added — the ROOT home (the host multiplexer). Before this
    # there was NO blessed restart for the root home at all: -Profile threw, and the
    # only thing that could bring the multiplexer back was a client-side start that
    # needs CREATE_BREAKAWAY_FROM_JOB and dies as windows_breakaway_unavailable
    # inside the Electron Job Object.
    [ValidateSet("all","default","pm-bangioi","tl-bangioi","testlead-bangioi","researchlead-bangioi","docwriter-bangioi","senior-techlead-bangioi","developer-bangioi","code-reviewer-bangioi","security-reviewer-bangioi","coo-bangioi","cpo-bangioi","cco-bangioi","")]
    [string]$Profile = "",

    [switch]$Force,

    [bool]$WaitVerify = $true,

    [int]$MaxWaitSeconds = 60,

    # Force the inline restart (stop, start, wait for LISTEN, health check)
    # instead of deferring 1 minute via schtasks. The deferred task that
    # Restart-GatewayDetached creates passes -Inline so the spawned task
    # performs the real restart. See Restart-GatewayDetached for why.
    [switch]$Inline,

    # Name of the deferred one-shot scheduled task this run should delete
    # when it finishes. Restart-GatewayDetached passes the task's OWN name,
    # so the task removes itself instead of lingering forever. The deletion
    # lives in a finally block around MAIN, so it runs on BOTH the success
    # and the failure path (the script ends in exit 0 / exit 1, and PowerShell
    # unwinds through finally on exit).
    [string]$CleanupTaskName = ""
)

$ErrorActionPreference = "Continue"

# --- Absolute path to the PowerShell 7 host binary ------------------------------
# WHY absolute: a Scheduled Task action that resolves 'pwsh' from PATH depends on
# the PATH the Task Scheduler happens to hand the task. Pinning the absolute path
# removes that dependency AND is required for the hidden form below.
# WHY the hidden form: a Scheduled Task whose principal is LogonType=InteractiveToken
# runs its action in the CEO's interactive desktop session. A CONSOLE-subsystem
# binary (powershell.exe / pwsh.exe / cmd.exe / python.exe) allocates a console
# host, and Windows RAISES that console window on the desktop -- the CEO sees a
# window steal the keyboard focus mid-sentence. -WindowStyle Hidden stops the
# console window from being shown.
$PWSH_EXE = 'C:\Program Files\PowerShell\7\pwsh.exe'
if (-not (Test-Path -LiteralPath $PWSH_EXE)) {
    # Fall back to PATH resolution only if the pinned binary is absent (a
    # non-standard install). The hidden flag below still applies.
    $PWSH_EXE = 'pwsh.exe'
}

# === AC2: Self-validation block (t_4139ff93, 2026-11-04) ===
# Print the executing path, SHA256 of own content, compare to the deployed
# copy, and WARN loudly on divergence. Warn-only, never exit (D5).
$executingPath = $PSCommandPath
if (-not $executingPath) { $executingPath = $MyInvocation.MyCommand.Path }
$deployedPath  = "C:\Users\dangv\AppData\Local\hermes\gateway-restart-remote.ps1"

if ($executingPath -and (Test-Path $executingPath)) {
    $ownHash = (Get-FileHash -Path $executingPath -Algorithm SHA256).Hash
    Write-Host "[self-validate] Executing: $executingPath"
    Write-Host "[self-validate] SHA256:    $ownHash"
    if (Test-Path $deployedPath) {
        $deployedHash = (Get-FileHash -Path $deployedPath -Algorithm SHA256).Hash
        if ($ownHash -ne $deployedHash) {
            Write-Host ""
            Write-Host "===========================================" -ForegroundColor Yellow
            Write-Host "  WARNING: DEPLOYED COPY DIVERGENCE" -ForegroundColor Yellow
            Write-Host "===========================================" -ForegroundColor Yellow
            Write-Host "  Executing: $executingPath" -ForegroundColor Yellow
            Write-Host "  SHA256:    $ownHash" -ForegroundColor Yellow
            Write-Host "  Deployed:  $deployedPath" -ForegroundColor Yellow
            Write-Host "  SHA256:    $deployedHash" -ForegroundColor Yellow
            Write-Host "  The deployed copy differs from the executing copy." -ForegroundColor Yellow
            Write-Host "  The deployed copy is the canonical runtime target (D1)." -ForegroundColor Yellow
            Write-Host "  This script will proceed, but the divergence should be" -ForegroundColor Yellow
            Write-Host "  investigated. Never sync-pull over the deployed copy (D3)." -ForegroundColor Yellow
            Write-Host "===========================================" -ForegroundColor Yellow
            Write-Host ""
        } else {
            Write-Host "[self-validate] Deployed copy matches executing copy." -ForegroundColor DarkGray
        }
    } else {
        Write-Host "[self-validate] Deployed copy not found at $deployedPath (skipping comparison)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "[self-validate] Could not determine executing path (skipping self-validation)" -ForegroundColor DarkGray
}
# === End AC2 self-validation ===

# --- Configuration ---
$HermesHome = "C:\Users\dangv\AppData\Local\hermes"

# ABI-matched launcher (incident 2026-11-15, pm-bangioi Telegram gateway dead).
# `hermes-agent\venv\Scripts\hermes.exe` is a CPython 3.11 shim, but the CLI
# injects the profile environment's CPython 3.14 site-packages, so
# `pydantic_core`'s cp314 extension cannot load and EVERY agent turn died with
#   ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'
# Resolve-HermesLauncher probes each candidate with the environment's
# site-packages on PYTHONPATH and THROWS when none is ABI-compatible
# (no silent fallback - PROCESS-KEY-PRINCIPLES.md §4).
. (Join-Path $PSScriptRoot 'resolve-hermes-launcher.ps1')
$HermesDir  = "$HermesHome\hermes-agent"

# LAZY launcher resolution - NEVER at load time (CI fix 2026-11-15).
# This script is spawned by the restart paths and dot-sourced by the Pester
# suites. Resolve-HermesLauncher THROWS by design on any host without the
# Windows Hermes home (the Linux CI runner, for one), so a load-time call
# killed the whole script there and broke the gateway-supervisor Pester suites
# (hermes-validation, run 43713301273, 2026-11-15). The launcher is therefore
# resolved from a function, never from the script body.
#
# WHERE it is resolved changed on 2026-11-26 (t_784ed637 P0): Start-Gateway's
# point-of-use resolution ran AFTER Stop-Gateway had already killed the lane, so a
# probe that could not answer left the lane DOWN with no dispatcher (measured: >7m50s
# of downtime, 22 ready cards, 0 spawns). Assert-LauncherResolvable now resolves the
# launcher BEFORE the first stop - at the run level and again per lane - and
# Get-HermesExe caches the answer, so the point-of-use call reads that cache instead of
# probing a stopped lane. The fail-closed guarantee is unchanged; it now fires while
# every lane is still UP.
$script:HermesExeCache = $null
$script:HermesExe      = $null

function Get-HermesExe {
    <#
    .SYNOPSIS
        Resolve (and cache) the ABI-matched hermes.exe launcher.
    .DESCRIPTION
        Lazy accessor for the launcher. THROWS - fail closed - when no
        ABI-compatible launcher exists, exactly like Resolve-HermesLauncher.
    .PARAMETER HermesHomeOverride
        Resolve against a different home. Used by the test suite to exercise the
        fail-closed path without touching the real install.
    #>
    param([string]$HermesHomeOverride)

    if ($script:HermesExeCache -and -not $HermesHomeOverride) { return $script:HermesExeCache }

    $homePath = if ($HermesHomeOverride) { $HermesHomeOverride } else { $HermesHome }
    $resolved = Resolve-HermesLauncher -HermesHome $homePath
    if (-not $HermesHomeOverride) {
        $script:HermesExeCache = $resolved
        $script:HermesExe      = $resolved
    }
    return $resolved
}

function Resolve-HermesExeForLane {
    <#
    .SYNOPSIS
        Lane-scoped launcher resolution that NEVER throws.
    .DESCRIPTION
        Get-HermesExe THROWS by design when no ABI-compatible launcher exists
        (No-Silent-Fallback, PROCESS-KEY-PRINCIPLES.md section 4). A throw escaping
        Start-Gateway aborts the restart wave: the profiles behind this one are never
        restarted, and on the supervisor path the same shape terminates the caretaker
        itself (t_b868309e item 1).

        Returns the launcher path, or $null after logging ERROR. The caller then
        fails THIS ONE profile closed (return $false) and the wave carries on.
    #>
    param([string]$Target = 'lane')

    try {
        return [string](Get-HermesExe)
    } catch {
        Write-Log "REFUSING to start ${Target}: no ABI-compatible hermes.exe launcher could be resolved: $($_.Exception.Message)" "ERROR"
        return $null
    }
}

# All known profiles -- order matches ``Get-TargetProfiles -ProfileName 'all'`` so
# sequential restart leaves healthy ports in canonical listening order.
# Wave 3.5 P0: senior entry renamed seniortechlead-bangioi -> senior-techlead-bangioi
# (canonical per W2.5 PR #17613).
$KnownProfiles = @(
    "default",
    "pm-bangioi",
    "tl-bangioi",
    "testlead-bangioi",
    "researchlead-bangioi",
    "docwriter-bangioi",
    "senior-techlead-bangioi",
    "developer-bangioi",
    "code-reviewer-bangioi",
    "security-reviewer-bangioi",
    "coo-bangioi",
    "cpo-bangioi",
    "cco-bangioi"
)

# Fallback table -- only consulted when config.yaml parsing fails (corrupt file,
# missing field). Authoritative source of truth is profiles/<name>/config.yaml.
$PortFallback = @{
    "default"                  = 9920
    "pm-bangioi"               = 9900
    "tl-bangioi"               = 9901
    "testlead-bangioi"         = 9902
    "researchlead-bangioi"     = 9903
    "docwriter-bangioi"        = 9904
    "senior-techlead-bangioi"  = 9906
    "developer-bangioi"        = 9905
    "code-reviewer-bangioi"    = 9910
    "security-reviewer-bangioi" = 9911
    "coo-bangioi"              = 9907
    "cpo-bangioi"              = 9908
    "cco-bangioi"              = 9909
}

# t_7973d4d5: the ROOT lane. Its home is %LOCALAPPDATA%\hermes itself — there is no
# profiles\default directory, and its config is the ROOT config.yaml. Every path
# helper below must agree on that, or a root restart would look at a directory that
# does not exist and report success while doing nothing.
$RootLaneName = "default"

# --- Single-owner guard (t_3a13db7b, P0) ---------------------------------------
# WHY this is dot-sourced rather than re-implemented: the guard classifies the live owner
# of an HERMES_HOME from gateway.lock + gateway_state.json + netstat -ano. A restart path
# that re-implements that classification drifts from the guard and starts refuting it
# (exactly how --force ended up spawning a second gateway on a live home). One definition,
# two callers.
#
# WHY it must run BEFORE the spawn: `hermes gateway run --force` treats a live owner as a
# WARNING ("--force: starting a second gateway although PID N owns this host"), so the
# admission check inside Hermes cannot stop a duplicate. The only place a duplicate can be
# stopped is here, in the spawner, before Start-Process.
$script:GatewaySingleOwnerGuardPath = ''
# Resolve candidates defensively. Join-Path ITSELF resolves the drive qualifier, so
# `Join-Path 'C:\Users\...\hermes' 'x.ps1'` THROWS DriveNotFoundException on a host that
# has no C: drive (the Linux CI runner, where $HermesHome is still the Windows literal)
# -- before Test-Path is ever reached. Use [System.IO.Path]::Combine instead: pure string
# joining, never touches the filesystem. Same precedent as Get-LaneHome in this file
# ("NOT `Join-Path $HermesHome` ... found by scripts-validation on the Linux runner,
# t_7973d4d5"). An unreachable candidate is then simply not a candidate.
$guardCandidates = New-Object System.Collections.Generic.List[string]
foreach ($guardBase in @($PSScriptRoot, $HermesHome)) {
    if (-not $guardBase) { continue }
    $guardCandidates.Add([System.IO.Path]::Combine($guardBase, 'gateway-single-owner.ps1'))
}
foreach ($candidate in $guardCandidates) {
    if (-not $candidate) { continue }
    # An unreachable candidate is simply not a candidate.
    $found = $false
    try { $found = [bool](Test-Path -LiteralPath $candidate) } catch { $found = $false }
    if ($found) {
        $script:GatewaySingleOwnerGuardPath = $candidate
        break
    }
}
if (-not $script:GatewaySingleOwnerGuardPath) {
    # No silent fallback: without the guard a duplicate spawn is undetectable, which is
    # the incident. Fail the restart instead of pretending the check ran.
    Write-Output "FATAL: gateway-single-owner.ps1 not found beside this script or in $HermesHome -- refusing to run unguarded"
    exit 1
}
. $script:GatewaySingleOwnerGuardPath

function Assert-LaneSpawnAllowed {
    <#
    .SYNOPSIS
        Pre-spawn admission for one lane. Returns the guard decision object.
    .DESCRIPTION
        Runs the single-owner guard against the lane home right before a `gateway run`
        would be spawned. Ok=$false means DO NOT SPAWN: a live owner is already serving
        this home and a second gateway would race it for state.db + the port, with the
        loser SIGKILLed and its workers dying `exit 130 [owner admission settled
        interrupted]`.

        -Reap is passed on the START path only: an orphan that holds state.db but binds no
        port must be cleared before the replacement can take the store.
    #>
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [switch]$Reap
    )

    # NB: do NOT name this $home — $HOME is a read-only automatic variable in
    # PowerShell, so `$home = ...` throws VariableNotWritable and the whole
    # Assert-LaneSpawnAllowed aborts BEFORE the spawn (observed 2026-11-18:
    # 7 lanes stayed down after a restart because -Force -Inline hit this).
    $laneHome = Get-LaneHome -Name $Name
    $port = Get-ProfilePort -Name $Name
    if ($port -le 0) {
        # Without a port the guard cannot tell "serving" from "orphan" -- say so instead
        # of spawning blind.
        return [pscustomobject]@{
            Ok = $false; Verdict = 'NO_PORT'; OwnerPid = 0; Reaped = @()
            Message = "REFUSE: no A2A port resolved for '$Name' -- cannot verify single-owner admission"
        }
    }

    $decision = Assert-GatewaySingleOwner -ProfileDir $laneHome -Port $port -Reap:$Reap
    if (-not $decision.Ok) {
        Write-Log "SINGLE-OWNER GUARD: $($decision.Message)" "ERROR"
    } elseif ($Reap -and @($decision.Reaped).Count -gt 0) {
        Write-Log "SINGLE-OWNER GUARD: reaped orphan pid(s) $(@($decision.Reaped) -join ', ') for $Name before spawn" "WARN"
    }
    return $decision
}

function Get-LaneHome {
    <#
    .SYNOPSIS
        Resolve the GATEWAY HOME for a lane name.
    .DESCRIPTION
        `default` -> the root home ($HermesHome). Anything else -> profiles\<name>.
    #>
    param([Parameter(Mandatory=$true)][string]$Name)
    if ($Name -eq $RootLaneName) { return $HermesHome }
    # NOT `Join-Path $HermesHome "profiles\$Name"`: on a non-Windows host Join-Path
    # throws "Cannot find drive. A drive with the name 'C' does not exist." and the
    # caller then binds a $null path to Test-Path (found by scripts-validation on the
    # Linux runner, t_7973d4d5). Path::Combine never touches the provider, so an
    # unresolvable home degrades to "path does not exist" — which the parity assert
    # already reports as an explicit WARN instead of a crash.
    return [System.IO.Path]::Combine($HermesHome, 'profiles', $Name)
}

function Get-LaneLaunchSpec {
    <#
    .SYNOPSIS
        Everything Start-Gateway needs to launch a lane, as data.
    .DESCRIPTION
        Extracted (t_7973d4d5) so the ROOT-lane shape is unit-testable without
        spawning anything, and so this tool and gateway-supervisor.ps1 cannot drift
        on the one question that matters: does the lane get `--profile` and an
        A2A_PORT override?

        ROOT (default):   home = $HermesHome, `--profile default` PINNED (F12), NO
                          A2A_PORT export (root config.yaml owns
                          gateway.platforms.a2a.extra.port), no --force.
        Profile lane:     home = profiles\<name>, `--profile <name>` PINNED (F12),
                          A2A_PORT exported, --force kept for the
                          unpublished-control-socket case (t_e4882d04).
    #>
    param([Parameter(Mandatory=$true)][string]$Name)

    # NB: do NOT name this $home — $HOME is a read-only automatic variable and the
    # assignment is a runtime error (caught by the t_7973d4d5 harness, would have
    # broken every Start-Gateway call).
    $laneHome = Get-LaneHome -Name $Name
    $port = Get-ProfilePort -Name $Name

    if ($Name -eq $RootLaneName) {
        return @{
            Home = $laneHome
            Port = $port
            IsRoot = $true
            A2aPortLine = "# A2A_PORT intentionally NOT exported (ROOT lane: config.yaml owns the port)"
            # F12 (ADR-gateway-lane-lifecycle.md, "THE ROOT CAUSE"): the selector is
            # MANDATORY. Without `--profile default` the child follows the sticky
            # <home>\active_profile (here: "pm-bangioi") and boots a duplicate
            # pm-bangioi that is refused, so :9920 never binds. Pinned, :9920 comes up.
            ArgumentLine = "gateway run --profile default --accept-hooks"
            LogName = "hermes-default.log"
        }
    }

    return @{
        Home = $laneHome
        Port = $port
        IsRoot = $false
        A2aPortLine = "`$env:A2A_PORT = '$port'"
        # F12 applies to EVERY lane, not only the ROOT one (review 5931797532,
        # t_20cd1a5e). Assert-LaneLaunchSpecPinned demands a selector for every lane
        # and Start-Gateway calls it BEFORE spawning, so an unpinned profile lane was
        # refused - and because Restart-Gateway stops the gateway first, the refusal
        # left that lane DOWN (measured: 12 of 13 lanes). Mirrors
        # gateway-supervisor.ps1's profile-lane Args (`--profile <name>`); --force is
        # kept for the unpublished-control-socket case (t_e4882d04).
        ArgumentLine = "gateway run --profile $Name --accept-hooks --force"
        LogName = "hermes-$Name.log"
    }
}

function Assert-LaneLaunchSpecPinned {
    <#
    .SYNOPSIS
        Fail closed when a lane launch spec carries no explicit profile selector.
    .DESCRIPTION
        Twin of gateway-supervisor.ps1's Assert-LaneStartSpecPinned, so the two tools
        cannot drift on F12 (ADR-gateway-lane-lifecycle.md): a gateway started without
        a selector follows <home>\active_profile, so the ROOT lane would boot a
        duplicate pm-bangioi and never bind :9920 (measured: 420 s wasted per attempt).
    #>
    param(
        [Parameter(Mandatory = $true)]$Launch,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $expected = if ($Name -eq $RootLaneName) { $RootLaneName } else { $Name }
    $tokens = @(([string]$Launch.ArgumentLine) -split '\s+' | Where-Object { $_ })
    $idx = [array]::IndexOf($tokens, '--profile')
    $got = if ($idx -ge 0 -and ($idx + 1) -lt $tokens.Count) { $tokens[$idx + 1] } else { $null }

    if ($null -eq $got) {
        throw ("REFUSE: lane '{0}' launch carries no --profile selector (argument line: '{1}'). Without it the gateway follows the sticky active_profile and boots the WRONG profile - ADR-gateway-lane-lifecycle.md F12, measured cost 420 s per attempt." -f $Name, $Launch.ArgumentLine)
    }
    if ($got -ne $expected) {
        throw ("REFUSE: lane '{0}' launches --profile '{1}' but must launch '{2}' (argument line: '{3}')." -f $Name, $got, $expected, $Launch.ArgumentLine)
    }
    return $true
}

function Assert-TargetLaunchSpecsPinned {
    <#
    .SYNOPSIS
        Fail closed for a WHOLE target list, before anything is stopped.
    .DESCRIPTION
        Stop-then-refuse hazard (review 5931797532, t_20cd1a5e): Restart-Gateway
        stops the live gateway BEFORE Start-Gateway re-validates the launch spec,
        so a spec defect used to leave the lane DOWN with nothing supervising it -
        and `-Profile all` did that to every lane it then refused. Validating the
        whole list up front aborts the run while every lane is still UP.
    #>
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($n in $Names) {
        $null = Assert-LaneLaunchSpecPinned -Launch (Get-LaneLaunchSpec -Name $n) -Name $n
    }
    return $true
}

function Assert-LauncherResolvable {
    <#
    .SYNOPSIS
        Fail closed when the ABI-matched launcher cannot be resolved - BEFORE anything is stopped.
    .DESCRIPTION
        Stop-then-never-start hazard, P0 (kanban t_784ed637; measured on this host
        2026-11-26 06:16-06:24 ICT):

          Stop-Gateway killed the live ROOT lane (15932 + 17676 gone - irreversible).
          Start-Gateway -> Get-HermesExe -> Resolve-HermesLauncher then spawned the ABI
          probe `python.exe -c "import pydantic_core, openai"`, which never returned
          (alive at 06:16:13, still alive when it was killed at 06:24:02: > 7 m 50 s) and
          the script's own retry hung the same way.

        The launcher was resolved AFTER the stop, so the lane stayed DOWN with no
        dispatcher for the whole hang: `gateway.log`'s last tick was 06:15:20 and the
        next one 06:28:53 - 22 ready cards, 0 spawns in between. A restart that cannot
        resolve its launcher must fail as a FAILED RESTART (every lane still UP), never
        as an outage.

        Two defences, in this order:
          1. THIS preflight - resolve the launcher BEFORE the first stop. Resolution is
             cached by Get-HermesExe, so Start-Gateway reuses this exact answer and does
             not re-probe after the stop.
          2. The probe itself is BOUNDED (resolve-hermes-launcher.ps1: staged
             WaitForExit ceilings, t_b868309e / t_9fa8ab06), so "cannot answer" is a
             bounded verdict instead of an unbounded wait - which is also why a hung or
             pathologically slow probe can no longer hold a lane down.
    .PARAMETER Names
        The lanes this run would restart. Named only so the log records what was protected.
    #>
    param([Parameter(Mandatory = $true)][string[]]$Names)

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $launcher = Get-HermesExe
    } catch {
        $stopwatch.Stop()
        throw ("REFUSE (launcher preflight, before any stop): no ABI-compatible hermes.exe could be resolved in {0} ms, so [{1}] were NOT stopped and are still UP. Cause: {2}" -f $stopwatch.ElapsedMilliseconds, (@($Names) -join ', '), $_.Exception.Message)
    }
    $stopwatch.Stop()

    if (-not $launcher) {
        throw ("REFUSE (launcher preflight, before any stop): Get-HermesExe returned an empty launcher, so [{0}] were NOT stopped and are still UP." -f (@($Names) -join ', '))
    }
    Write-Log ("launcher preflight OK for [{0}]: '{1}' resolved in {2} ms - no lane has been stopped yet" -f (@($Names) -join ', '), $launcher, $stopwatch.ElapsedMilliseconds) "INFO"
    return [string]$launcher
}

function Get-GatewayStandaloneFlag {
    <#
    .SYNOPSIS
        Read `gateway.standalone` out of a config.yaml.
    .DESCRIPTION
        $true  -> the key is present and true
        $false -> present-and-false, OR ABSENT (absent is the value that broke the
                  multiplexer on 2026-11-14, so it must never be read as "unknown")
        $null  -> file missing or unreadable (caller warns; never counts as a pass)

        Twin of gateway-supervisor.ps1's Get-ProfileStandaloneFlag. Both are covered
        by the same Pester cases (hermes/tests/gateway-supervisor-root-lane.Tests.ps1)
        so the two tools cannot drift.
    #>
    param([Parameter(Mandatory=$true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { $lines = @(Get-Content -LiteralPath $Path -ErrorAction Stop) } catch { return $null }

    $gwIndent = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ($line -match '^\s*#') { continue }
        if ($gwIndent -lt 0) {
            if ($line -match '^([ \t]*)gateway:\s*$') { $gwIndent = $Matches[1].Length }
            continue
        }
        if (-not ($line -match '^([ \t]*)\S')) { continue }
        $indent = $Matches[1].Length
        if ($indent -le $gwIndent) { break }
        if ($line -match '^\s*standalone:\s*([^\s#]+)') {
            $v = $Matches[1].Trim().Trim("'").Trim('"').ToLowerInvariant()
            return ($v -eq 'true')
        }
    }
    return $false
}

function Assert-SupervisorProfileParity {
    <#
    .SYNOPSIS
        Fail-loud self-test: this tool's profile set must cover every profile
        the supervisor supervises, and every name must be parseable.

    .DESCRIPTION
        Root cause of kanban t_e4882d04 (P0, 2026-11-11): gateway-supervisor
        $Profiles grew developer-bangioi/code-reviewer-bangioi/
        security-reviewer-bangioi but this tool's ValidateSet and canonical
        lists were never updated, so -Profile <name> threw and -Profile all
        silently skipped them — 3 lanes could never be restarted and the
        elevated-gateway recovery runbook was dead in the water.

        PARITY RULE: every profile in the supervisor's $Profiles MUST appear in
        $KnownProfiles (the source of truth for Get-TargetProfiles / -Profile
        'all'). ValidateSet is a compile-time literal and duplicates this list
        — both must be edited together (see comment at the param block).
        The Pester suite (scripts/tests/gateway-restart-port-mapping.Tests.ps1)
        additionally asserts ValidateSet ⊇ $KnownProfiles.

        t_7973d4d5: also asserts the two host-wide lane invariants that cost the host
        hours on 2026-11-14 — standalone parity (every supervised lane that owns its
        own daemon must carry gateway.standalone: true; the ROOT lane must not) and
        port disjointness (no two lanes, and not the ROOT config, may resolve to one
        port). Both THROW; see the block below.

    .PARAMETER SupervisorPath
        Override for gateway-supervisor.ps1. Defaults to the sibling script. Only
        exists so hermes/tests can point the assert at a fixture lane table; a
        production caller never passes it.
    #>
    param([string]$SupervisorPath = "")

    if (-not $SupervisorPath) { $SupervisorPath = Join-Path $PSScriptRoot "gateway-supervisor.ps1" }
    $supervisorPath = $SupervisorPath
    if (-not (Test-Path $supervisorPath)) {
        Write-Log "Assert-SupervisorProfileParity: supervisor script not found at $supervisorPath - parity check skipped" "WARN"
        return $true
    }
    try {
        $sup = Get-Content $supervisorPath -Raw
        # Names live in $Profiles as: @{ Name = "<name>"; Port = ... }
        $supNames = @([regex]::Matches($sup, '(?m)^\s*@\{\s*Name\s*=\s*"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
        if ($supNames.Count -eq 0) {
            Write-Log "Assert-SupervisorProfileParity: parsed 0 supervisor profiles - check regex against gateway-supervisor.ps1" "WARN"
            return $true
        }
        $missing = @($supNames | Where-Object { $KnownProfiles -notcontains $_ })
        if ($missing.Count -gt 0) {
            throw ("Profile drift: supervisor supervises [{0}] but gateway-restart-remote.ps1 does not know them. Add each to `$KnownProfiles AND the [ValidateSet(...)] literal." -f ($missing -join ', '))
        }
        $unparseable = @($KnownProfiles | Where-Object { -not $PortFallback.ContainsKey($_) })
        if ($unparseable.Count -gt 0) {
            throw ("PortFallback drift: [$($unparseable -join ', ')] in `$KnownProfiles has no fallback port. Add a $PortFallback entry (9905/9910/9911 precedent — t_e4882d04).")
        }

        # --- t_7973d4d5: standalone parity + port disjointness -----------------
        # Two host-wide invariants, each of which cost the host hours on 2026-11-14:
        #
        #   STANDALONE PARITY  every supervised lane that owns its own daemon must have
        #                      `gateway.standalone: true` in profiles/<lane>/config.yaml,
        #                      and the ROOT lane must NOT. One missing flag made the
        #                      multiplexer's all-or-nothing profile reserve exit 1 and
        #                      stranded a gateway.lock in 33 profile homes, so every
        #                      worker it served died windows_breakaway_unavailable.
        #   PORT DISJOINTNESS  two lanes (or the ROOT config's a2a port and a lane) may
        #                      not resolve to one port. The root home held 9900 == the
        #                      pm-bangioi lane port, so every pm-bangioi restart killed
        #                      the multiplexer through the old blind kill-by-port leg.
        #
        # Both THROW on purpose. This is the tool an operator reaches for when gateways
        # are down; half-restarting a host that violates these invariants is exactly how
        # the 10:30 kill happened. Fix the config first.
        $rootLaneFromTable = $null
        foreach ($m in [regex]::Matches($sup, '(?m)^\s*@\{\s*Name\s*=\s*"([^"]+)"([^\r\n]*)')) {
            if ($m.Groups[2].Value -match 'Root\s*=\s*\$true') { $rootLaneFromTable = $m.Groups[1].Value }
        }

        $portOwners = @{}
        foreach ($n in $supNames) {
            $p = Get-ProfilePort -Name $n
            if (-not $p) { continue }
            $pi = [int]$p
            if ($portOwners.ContainsKey($pi)) {
                throw ("Port collision: supervised lanes '{0}' and '{1}' both resolve to port {2}. A lane stop clears that port, so one lane's restart would kill the other — the 2026-11-14 10:30 multiplexer death. Reassign a port in one config.yaml." -f $portOwners[$pi], $n, $pi)
            }
            $portOwners[$pi] = $n
        }

        foreach ($n in $supNames) {
            $isRootLane = ($n -eq $RootLaneName) -or ($n -eq $rootLaneFromTable)
            # A lane home that does not resolve is a WARN, never a crash: this assert
            # runs on the Linux CI runner too, where the production home
            # (C:\Users\...) is not a real path. Binding a $null -Path here used to
            # throw ParameterBindingValidationException and redden the whole suite.
            $laneHome = $null
            try { $laneHome = Get-LaneHome -Name $n } catch { $laneHome = $null }
            if (-not $laneHome) {
                Write-Log "Standalone parity: cannot resolve the home for supervised lane '$n' - cannot verify (NOT counted as a pass)" "WARN"
                continue
            }
            $cfg = [System.IO.Path]::Combine($laneHome, 'config.yaml')
            if (-not (Test-Path -LiteralPath $cfg)) {
                Write-Log "Standalone parity: supervised lane '$n' has no config.yaml at $cfg - cannot verify (NOT counted as a pass)" "WARN"
                continue
            }
            $standalone = Get-GatewayStandaloneFlag -Path $cfg
            if ($null -eq $standalone) {
                Write-Log "Standalone parity: supervised lane '$n' config.yaml unreadable at $cfg - cannot verify (NOT counted as a pass)" "WARN"
                continue
            }
            if ($isRootLane -and $standalone) {
                throw ("Standalone parity: lane '{0}' is the ROOT lane (the host multiplexer) but {1} sets gateway.standalone: true. The multiplexer must serve profiles, not reserve one for itself — remove the key." -f $n, $cfg)
            }
            if ((-not $isRootLane) -and (-not $standalone)) {
                throw ("Standalone parity: supervised lane '{0}' owns its own daemon but {1} has no 'gateway.standalone: true'. The multiplexer's all-or-nothing profile reserve will exit 1 and strand a gateway.lock in every profile it serves (2026-11-14: security-reviewer-bangioi). Add the flag or drop the lane from gateway-supervisor.ps1 `$Profiles." -f $n, $cfg)
            }
        }

        Write-Log "Profile parity OK: $($supNames.Count) supervisor profiles all restartable; $($KnownProfiles.Count) known profiles all port-mapped; standalone + port disjointness verified" "INFO"
        return $true
    } catch [System.Management.Automation.RuntimeException] {
        throw
    } catch {
        Write-Log "Assert-SupervisorProfileParity skipped (parse error: $_)" "WARN"
        return $true
    }
}

function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] [$Level] $Msg"
}

# --- FIX (t_d4283b6b, 2026-11-02): scrub worker-session env vars before
# --- spawning gateways. Same rationale as gateway-supervisor.ps1: when a
# --- kanban worker invokes this script, the child pwsh.exe inherits the
# --- worker's full environment including HERMES_DELEGATED_CHILD_CONTEXT,
# --- which fences the gateway's kanban dispatcher.
function Remove-WorkerEnvVars {
    $workerVars = @(
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_DISPATCHER",
        "HERMES_PROFILE",
        "HERMES_SPAWN_MODE",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_SOURCE",
        "HERMES_CRON_ID",
        "HERMES_CRON_SESSION_KEY"
    )
    foreach ($v in $workerVars) {
        if ($null -ne [System.Environment]::GetEnvironmentVariable($v, "Process")) {
            [System.Environment]::SetEnvironmentVariable($v, $null, "Process")
        }
    }
}

function Get-ProfilePort {
    <#
    .SYNOPSIS
        Resolve the canonical A2A port for a profile.

    .DESCRIPTION
        Parses profiles/<name>/config.yaml for the first integer after a
        top-level ``port:`` key. Falls back to $PortFallback if the file is
        missing, unreadable, or contains no usable port field.
    #>
    param([Parameter(Mandatory=$true)][string]$Name)

    # t_7973d4d5: the ROOT lane's port lives in the ROOT config.yaml
    # (gateway.platforms.a2a.extra.port — the only bare `port:` key there), not in a
    # profile config. Same parser, different file.
    $configPath = if ($Name -eq $RootLaneName) {
        [System.IO.Path]::Combine($HermesHome, 'config.yaml')
    } else {
        # Path::Combine, not Join-Path: Join-Path throws "Cannot find drive 'C'" on the
        # Linux CI runner, leaving $configPath null (t_7973d4d5).
        [System.IO.Path]::Combine($HermesHome, 'profiles', $Name, 'config.yaml')
    }
    if ($configPath -and (Test-Path $configPath)) {
        try {
            $lines = Get-Content $configPath -ErrorAction SilentlyContinue
            foreach ($line in $lines) {
                if ($line -match '^\s*port\s*:\s*(\d{2,5})\s*$') {
                    $parsed = [int]$Matches[1]
                    if ($parsed -ge 1024 -and $parsed -le 65535) {
                        return $parsed
                    }
                }
            }
        } catch {
            Write-Log "config.yaml parse failed for $Name (using fallback): $_" "WARN"
        }
    }
    return $PortFallback[$Name]
}

function Test-PortListening {
    param([int]$Port)
    try {
        # Get-NetTCPConnection can return a single object or an array depending
        # on result count. -State Listen is supported but the parameter is
        # fragile under Pester mocks; rely on .State filter on the result.
        $conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        if ($null -eq $conn) { return $false }
        $rows = @($conn)
        # Method 2 (wrap Where-Object output in @()) reliably returns an array
        # with .Count. Method 1 (bare .Count after parens) silently returns
        # $null in some PowerShell hosts -- see probe-w2.ps1 in repo notes.
        $listen = @($rows | Where-Object { $_.State -eq 'Listen' })
        return ($rows.Count -ge 1 -and $listen.Count -ge 1)
    } catch {
        # Get-NetTCPConnection can fail under restricted permissions; fall back to a TCP probe.
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
            $ok = $iar.AsyncWaitHandle.WaitOne(500, $false)
            if ($ok) {
                $client.EndConnect($iar)
                $client.Close()
                return $true
            }
            $client.Close()
            return $false
        } catch { return $false }
    }
}

function Get-TargetProfiles {
    param([string]$ProfileName)
    # Build the result as a local variable first, then output with -NoEnumerate
    # to defeat PowerShell's pipeline-array-unrolling behavior on `return`.
    # t_e4882d04: both branches below are hard-coded literals that drifted from
    # $KnownProfiles (and from supervisor $Profiles) — 3 lane profiles were
    # un-restartable. They now DERIVE from $KnownProfiles: one edit point.
    # Order of $KnownProfiles is the canonical listening order.
    if ($ProfileName -eq "all") {
        $result = @($KnownProfiles)
        Write-Output -NoEnumerate $result
        return
    }
    if ($KnownProfiles -notcontains $ProfileName) {
        throw "Unknown profile '$ProfileName'. Known: $($KnownProfiles -join ', ')"
    }
    # Single-profile case: explicitly wrap in array (NOT a char-array).
    $result = @($ProfileName)
    Write-Output -NoEnumerate $result
}

function Test-GatewayHealthy {
    param([string]$Name)
    $lockPath = Join-Path (Get-LaneHome -Name $Name) 'gateway.lock'
    if (-not (Test-Path $lockPath)) { return $false }
    try {
        $lock = Get-Content $lockPath -Raw | ConvertFrom-Json
        $proc = Get-Process -Id $lock.pid -ErrorAction SilentlyContinue
        return ($null -ne $proc)
    } catch { return $false }
}

# t_4aa936ec review (P1): the planned-stop window is DERIVED, not hard-coded.
#
# The first cut waited 20 s. That is SHORTER than the drain budget of the handler the
# marker invokes: gateway/run_shutdown.py drains for agent.cron_drain_timeout whenever a
# cron job or api_server run is in flight (default 30 s -- hermes_cli/config_defaults.py)
# and agent.restart_drain_timeout (default 0) for chat turns, with the shutdown watchdog
# armed at drain + 60 s grace (gateway/shutdown_watchdog.py). A routine restart during an
# executing cron legitimately drains ~30 s, so a 20 s ask expired mid-drain and this script
# took its FORCE-FALLBACK branch -- the exact unclean exit the marker exists to remove.
#
# Floor  : core's cron drain floor (30 s) + headroom (marker write, 0.5 s poll, handler
#          tail) = 45 s.
# Ceiling: below core's .gateway-planned-stop.json TTL -- gateway/status.py
#          (_PLANNED_STOP_MARKER_TTL_S = 60). A marker older than its TTL matches NOBODY,
#          so a wait longer than the TTL could never be drained; 55 s keeps a 5 s margin.
#
# Kept IDENTICAL to gateway-supervisor.ps1's copy on purpose (same numbers, same parse):
# both tools stop the same lanes, so a window that differs between them would make the
# drain outcome depend on which tool asked.
function Get-PlannedStopDrainSeconds {
    param([string]$LaneHome)

    $drain = 30   # core: agent.cron_drain_timeout default (hermes_cli/config_defaults.py)
    if (-not $LaneHome) { return $drain }
    $cfg = [System.IO.Path]::Combine($LaneHome, 'config.yaml')
    $found = $false
    try { $found = [bool](Test-Path -LiteralPath $cfg) } catch { $found = $false }
    if (-not $found) { return $drain }
    try { $lines = @(Get-Content -LiteralPath $cfg -ErrorAction Stop) } catch { return $drain }

    # Text parse on purpose: no YAML dependency (same rule as the supervisor's copy).
    $agentIndent = -1
    foreach ($line in $lines) {
        if ($line -match '^\s*#') { continue }
        if ($agentIndent -lt 0) {
            if ($line -match '^([ \t]*)agent:\s*$') { $agentIndent = $Matches[1].Length }
            continue
        }
        if (-not ($line -match '^([ \t]*)\S')) { continue }
        if ($Matches[1].Length -le $agentIndent) { break }   # left the agent: block
        if ($line -match '^\s*(?:cron_drain_timeout|restart_drain_timeout):\s*([0-9]+)\s*(?:#.*)?$') {
            $value = [int]$Matches[1]
            if ($value -gt $drain) { $drain = $value }
        }
    }
    return $drain
}

function Get-PlannedStopGraceSeconds {
    param([string]$LaneHome)

    $headroom = 15    # marker write + 0.5 s poll + shutdown-handler tail
    $markerTtl = 60   # gateway/status.py::_PLANNED_STOP_MARKER_TTL_S
    $want = (Get-PlannedStopDrainSeconds -LaneHome $LaneHome) + $headroom
    $floor = 45       # 30 s cron drain floor + the same headroom
    if ($want -lt $floor) { $want = $floor }
    if ($want -gt ($markerTtl - 5)) { $want = $markerTtl - 5 }
    return $want
}

# t_4aa936ec review (P2): re-prove identity IMMEDIATELY before a FORCE-FALLBACK kill.
#
# The fallback fires on a claimant set computed a whole planned-stop grace window earlier
# (>= 45 s), so a pid can have been RECYCLED inside that window -- the exact residual
# SR-19092-02 accepted for 2 s, widened to 45+ s by the fallback. The supervisor's retry
# path already re-runs Test-LaneKillCandidate before its kill; this script has no such
# guard function, so the recheck below is this script's OWN stop criterion re-taken:
#   * the pid must still be a LIVE claimant of THIS home per a FRESH owner snapshot
#     (Get-GatewayOwnerSnapshot proves lock/state/heartbeat attribution and rejects a
#     recycled pid whose start time is >120 s away from the record's), and
#   * when the pid came from gateway.lock, that lock must STILL name it and the process
#     must have been running when the lock was written (a live process at that pid which
#     STARTED AFTER the lock was written cannot be the process that wrote it).
# A candidate that fails is REFUSED, never killed (fail closed).
function Test-RemoteKillCandidate {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ProfileDir,
        [int]$Port = 0,
        # Nonzero => this pid is the lane's own gateway.lock pid; the lock must still
        # describe the live process (SR-19092-01's proof, re-taken).
        [int]$LockPid = 0
    )

    $result = @{ Safe = $false; Reason = 'not-evaluated' }
    if ($ProcessId -le 0) { $result.Reason = 'invalid-pid'; return $result }

    if ($LockPid -gt 0) {
        $lockPath = [System.IO.Path]::Combine($ProfileDir, 'gateway.lock')
        $lockExists = $false
        try { $lockExists = [bool](Test-Path -LiteralPath $lockPath) } catch { $lockExists = $false }
        if (-not $lockExists) { $result.Reason = 'lock-missing'; return $result }
        $lock = $null
        try { $lock = Get-Content -LiteralPath $lockPath -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $lock = $null }
        if (-not $lock -or -not $lock.pid) { $result.Reason = 'lock-pid-missing'; return $result }
        if ([int]$lock.pid -ne $ProcessId) { $result.Reason = 'lock-pid-changed'; return $result }
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) { $result.Reason = 'pid-not-live'; return $result }
        try {
            $startedAt = $proc.StartTime.ToUniversalTime()
            $lockAt = (Get-Item -LiteralPath $lockPath -ErrorAction Stop).LastWriteTime.ToUniversalTime()
            # 5 s tolerance is the supervisor's own (`$skewSeconds -lt -5`): a lock is
            # written a few seconds after the gateway starts, never before it.
            if (($startedAt - $lockAt).TotalSeconds -gt 5) { $result.Reason = 'lock-pid-recycled'; return $result }
        } catch { }
    }

    if ($Port -gt 0) {
        Clear-GatewayProcessTableCache
        $snap = $null
        try { $snap = Get-GatewayOwnerSnapshot -ProfileDir $ProfileDir -Port $Port } catch { $snap = $null }
        if (-not $snap) { $result.Reason = 'snapshot-unavailable'; return $result }
        if (@($snap.LiveOwnerPids) -notcontains $ProcessId) { $result.Reason = 'not-a-live-claimant'; return $result }
    } else {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { $result.Reason = 'pid-not-live'; return $result }
    }

    $result.Safe = $true
    $result.Reason = 'verified'
    return $result
}

# t_4aa936ec: ask the lane to stop, and only then force it.
#
# Stop-Gateway used to unlink .gateway-planned-stop.json and call Stop-Process
# (TerminateProcess) on every claimant. On native Windows TerminateProcess runs NO exit
# path, so every routine restart produced "Previous gateway life ... exited UNCLEANLY",
# killed the dispatcher's worker children (task_runs outcome=owner_lost) and armed the
# 1800 s deferred state.db integrity check on the next boot. Core implements the clean
# stop: gateway/run.py::_run_planned_stop_watcher polls .gateway-planned-stop.json every
# 0.5 s and invokes the SAME shutdown handler as SIGTERM (drain + resume_pending); on
# Windows that marker is the ONLY clean stop, because asyncio.add_signal_handler raises
# NotImplementedError there (issue #33778). So: write the marker, wait, and force only on
# timeout -- loudly, because only that fallback is unclean.
function Invoke-PlannedStop {
    param(
        [Parameter(Mandatory = $true)][string]$LaneHome,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [string]$Name = '',
        # 0 = DERIVE from core's own drain budget (Get-PlannedStopGraceSeconds).
        [int]$MaxWaitSeconds = 0
    )

    if ($MaxWaitSeconds -le 0) { $MaxWaitSeconds = Get-PlannedStopGraceSeconds -LaneHome $LaneHome }

    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }

    $markerPath = Join-Path $LaneHome '.gateway-planned-stop.json'
    # target_start_time is null on purpose: gateway.status._pid_marker_names_self falls
    # back to PID equality when a start time is unknown (its Windows case), so the marker
    # names the process without this script reproducing the platform primitive.
    $marker = [ordered]@{
        target_pid        = $ProcessId
        target_start_time = $null
        stopper_pid       = $PID
        written_at        = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffff+00:00')
        reason            = 'restart-remote-planned-stop'
    }
    try {
        [System.IO.File]::WriteAllText(
            $markerPath,
            ($marker | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-Log "PLANNED-STOP: cannot write $markerPath ($($_.Exception.Message)) - hard stop only" "WARN"
        return $false
    }

    $ticks = 0
    while ($ticks -lt ($MaxWaitSeconds * 2)) {
        Start-Sleep -Milliseconds 500
        $ticks++
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            Write-Log ("PLANNED-STOP: $Name pid $ProcessId drained and exited on the marker after " +
                "$([math]::Round($ticks / 2, 1))s - clean exit path ran, no UNCLEAN record") "INFO"
            return $true
        }
    }

    if (Test-Path $markerPath) { Remove-Item $markerPath -Force -ErrorAction SilentlyContinue }
    return $false
}

function Stop-Gateway {
    param([string]$Name, [switch]$Hard)

    $laneHome = Get-LaneHome -Name $Name
    $lockPath = Join-Path $laneHome 'gateway.lock'
    $port = Get-ProfilePort -Name $Name
    # t_4aa936ec review (P1): ONE derived window for the whole stop, resolved here so both
    # planned-stop asks and both FORCE-FALLBACK log lines report the same number that ran.
    $plannedStopWindow = Get-PlannedStopGraceSeconds -LaneHome $laneHome

    # t_4aa936ec: a stale planned-stop marker is left to the gateway: gateway.status
    # validates it against the live PID + a 60 s TTL and unlinks stale/malformed ones.
    # Unlinking it here destroyed the signal that makes a stop legitimate.
    $plannedStop = Join-Path $laneHome '.gateway-planned-stop.json'

    # t_3a13db7b (AC2): stop EVERY claimant, not just lock.pid. The duplicate-spawn
    # incident ran two gateways on one home; killing only the pid in gateway.lock leaves
    # the other one alive, and the following Start-Gateway then stacks a THIRD process on
    # a home that was never actually vacated. The guard knows every claimant, so ask it.
    $stoppedPids = @()
    # t_4aa936ec review (P2): pids whose force-kill was REFUSED by the re-check. They must
    # not be killed by the trailing sweep either - otherwise the refusal is theatre: the
    # sweep three seconds later would terminate exactly what the proof declined to.
    $refusedPids = @()
    if ($port -gt 0) {
        $snap = Get-GatewayOwnerSnapshot -ProfileDir $laneHome -Port $port
        foreach ($claimPid in @($snap.LiveOwnerPids)) {
            if ((-not $claimPid) -or ([int]$claimPid -le 0)) { continue }
            if (-not $Hard) {
                # t_4aa936ec: routine restart -> ask the claimant to drain (planned-stop).
                $drained = $false
                try { $drained = Invoke-PlannedStop -LaneHome $laneHome -ProcessId ([int]$claimPid) -Name $Name -MaxWaitSeconds $plannedStopWindow } catch { $drained = $false }
                if (-not $drained) {
                    # t_4aa936ec review (P2): the claimant set above is >= 45 s stale by
                    # now, so the pid may have been RECYCLED. Re-prove it immediately
                    # before the kill and REFUSE-KILL when the proof no longer holds.
                    $recheck = Test-RemoteKillCandidate -ProcessId ([int]$claimPid) -ProfileDir $laneHome -Port $port
                    if (-not $recheck.Safe) {
                        Write-Log "REFUSE-KILL (force-fallback re-check failed): $Name pid $claimPid ($($recheck.Reason)) - not proved to be this lane's gateway, so the planned-stop expiry is NOT used as licence to force-kill it" "WARN"
                        $refusedPids += [int]$claimPid
                        continue
                    } else {
                        Write-Log "FORCE-FALLBACK: $Name pid $claimPid did not exit within the ${plannedStopWindow}s planned-stop grace window - Stop-Process -Force (TerminateProcess); THIS exit is the unclean one" "WARN"
                        Stop-Process -Id ([int]$claimPid) -Force -ErrorAction SilentlyContinue
                    }
                }
            } else {
                Stop-Process -Id ([int]$claimPid) -Force -ErrorAction SilentlyContinue
            }
            $stoppedPids += [int]$claimPid
        }
        Clear-GatewayProcessTableCache
    }

    if (Test-Path $lockPath) {
        try {
            $lock = Get-Content $lockPath -Raw | ConvertFrom-Json
            if ($lock.pid -and ($stoppedPids -notcontains [int]$lock.pid)) {
                if (-not $Hard) {
                    $drained = $false
                    try { $drained = Invoke-PlannedStop -LaneHome $laneHome -ProcessId ([int]$lock.pid) -Name $Name -MaxWaitSeconds $plannedStopWindow } catch { $drained = $false }
                    if (-not $drained) {
                        # t_4aa936ec review (P2): same recheck for the LOCK pid - the lock
                        # must STILL name this pid and the process must predate it.
                        $recheck = Test-RemoteKillCandidate -ProcessId ([int]$lock.pid) -ProfileDir $laneHome -Port $port -LockPid ([int]$lock.pid)
                        if (-not $recheck.Safe) {
                            Write-Log "REFUSE-KILL (force-fallback re-check failed): $Name lock pid $($lock.pid) ($($recheck.Reason)) - not proved to be this lane's gateway, so the planned-stop expiry is NOT used as licence to force-kill it" "WARN"
                            $refusedPids += [int]$lock.pid
                        } else {
                            Write-Log "FORCE-FALLBACK: $Name lock pid $($lock.pid) did not exit within the ${plannedStopWindow}s planned-stop grace window - Stop-Process -Force (TerminateProcess); THIS exit is the unclean one" "WARN"
                            Stop-Process -Id $lock.pid -Force -ErrorAction SilentlyContinue
                        }
                    }
                } else {
                    Stop-Process -Id $lock.pid -Force -ErrorAction SilentlyContinue
                }
                if ($refusedPids -notcontains [int]$lock.pid) { $stoppedPids += [int]$lock.pid }
            }
        } catch { }
    }

    Start-Sleep -Seconds 3
    foreach ($pid0 in @($stoppedPids)) {
        if (Get-Process -Id $pid0 -ErrorAction SilentlyContinue) {
            Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
    Clear-GatewayProcessTableCache

    # NOTE: Do NOT kill by process name pattern -- hermes spawns hermes.exe + python.exe
    # from venv directory, NOT profiles directory. The claimant pids above are authoritative.

    # Wait for port to free (polls TCP listen state, not just TIME_WAIT expiry)
    $waited = 0
    while ($waited -lt 15) {
        if (-not (Test-PortListening -Port $port)) { break }
        Start-Sleep -Seconds 1
        $waited++
    }

    # t_3a13db7b (AC2): PROVE the home is vacated before declaring the stop successful.
    # "I sent Stop-Process" is not evidence. Re-snapshot: a live claimant, or a listener
    # still bound, means the restart must not spawn on top of it.
    $vacated = $true
    $residual = ''
    if ($port -gt 0) {
        $after = Get-GatewayOwnerSnapshot -ProfileDir $laneHome -Port $port
        if (@($after.LiveOwnerPids).Count -gt 0) {
            $vacated = $false
            $residual = "pid(s) $(@($after.LiveOwnerPids) -join ', ') still claim the home ($($after.Verdict))"
        } elseif ($after.PortListening) {
            $vacated = $false
            $residual = "port $port still LISTENING under pid $($after.PortOwnerPid)"
        }
    }

    if ($vacated) {
        if (Test-Path $lockPath) { Remove-Item $lockPath -Force -ErrorAction SilentlyContinue }
        Write-Log "Stopped $Name (port $port freed after ${waited}s; stopped pid(s): $(@($stoppedPids) -join ', '))" "SUCCESS"
        return $true
    }

    Write-Log "Stop of $Name did NOT vacate the home: $residual -- leaving gateway.lock in place as evidence" "ERROR"
    return $false
}

function Start-Gateway {
    param([string]$Name)

    $port = Get-ProfilePort -Name $Name
    $profileDir = Get-LaneHome -Name $Name
    $isRoot = ($Name -eq $RootLaneName)

    # Single-owner admission, BEFORE anything is spawned (t_3a13db7b).
    # -Reap: an orphan that holds state.db but binds no port is measured on this host
    # (code-reviewer pid 25676 alive + fresh heartbeat while :9910 was DOWN). It serves
    # nothing, but it still owns the store -- clear it, then start the replacement.
    # A SERVING owner is NOT reaped: that is the duplicate spawn this guard exists to stop.
    $admission = Assert-LaneSpawnAllowed -Name $Name -Reap
    if (-not $admission.Ok) {
        Write-Log "REFUSING to start ${Name}: $($admission.Message)" "ERROR"
        return $false
    }

    # Clean state
    $plannedStop = Join-Path $profileDir '.gateway-planned-stop.json'
    if (Test-Path $plannedStop) { Remove-Item $plannedStop -Force -ErrorAction SilentlyContinue }
    $stateFile = "$profileDir\gateway_state.json"
    if (Test-Path $stateFile) { Remove-Item $stateFile -Force -ErrorAction SilentlyContinue }

    # Create temp script
    # HERMES_BIN added for PM-2 fix: gateway dispatcher spawns kanban workers
    # via _resolve_hermes_argv() which reads HERMES_BIN first. Without it
    # sys.executable resolves to bare tools/python-3.14.7 (no hermes_cli),
    # causing No module hermes_cli.main on every worker.
    # FIX (t_d4283b6b): scrub worker-session env vars so the spawned gateway
    # does not inherit HERMES_DELEGATED_CHILD_CONTEXT etc. from a kanban worker
    # that invoked this script.
    Remove-WorkerEnvVars

    # t_e4882d04 Layer-2 fix (mirrors gateway-supervisor.ps1 Start-Gateway):
    # Stop-Gateway above already killed THIS profile's old owner and waited for
    # its port to free, so a sequential restart hits the host-ownership gate
    # ("A gateway already owns this host and has not published its served set
    # yet") whenever the previous spawn's control socket is still unpublished.
    # t_3a13db7b: --force is now gated by the single-owner guard in Start-Gateway
    # (Assert-LaneSpawnAllowed runs BEFORE the spawn and refuses on a live owner), so
    # "--force starting a second gateway although PID N owns this host" can no longer
    # happen from this script. --force is kept ONLY as the control-socket escape hatch.
    # t_e4882d04 is therefore: --force lets a fresh process take the host role when the
    # previous owner was already stopped by Stop-Gateway.
    # t_7973d4d5: shared shape decision (mirrors gateway-supervisor.ps1
    # Get-LaneStartSpec so the two tools cannot disagree). The ROOT lane is started
    # WITHOUT --force: --force says "start even though a gateway already owns this
    # host", which is the right escape hatch for a standalone per-port profile whose
    # control socket is unpublished, and the wrong one for the multiplexer — a second
    # host owner would fight the live one for every profile it serves.
    $launch = Get-LaneLaunchSpec -Name $Name
    # F12 fail-closed (mirrors gateway-supervisor.ps1's Start-Gateway): refuse to
    # spawn an unpinned lane rather than boot a duplicate of active_profile.
    $null = Assert-LaneLaunchSpecPinned -Launch $launch -Name $Name

    # ABI-matched launcher, resolved NOW (point of use) - fail closed if none.
    # NOT at load time: this script is spawned by the restart paths (and by the
    # Pester suites) and the CI runner has no Windows Hermes home, where the
    # resolver throws by design (hermes-validation, 2026-11-15).
    # t_b868309e item 1: routed through Resolve-HermesExeForLane so a resolution
    # failure logs ERROR and returns $false for THIS profile only, instead of a
    # throw aborting the whole restart wave.
    $HermesExe = Resolve-HermesExeForLane -Target $Name
    if (-not $HermesExe) { return $false }

    $startScript = @"
`$env:HERMES_HOME = '$($launch.Home)'
$($launch.A2aPortLine)
`$env:HERMES_BIN = '$HermesExe'
Set-Location '$HermesDir'
& '$HermesExe' $($launch.ArgumentLine) 2>&1 | Tee-Object -FilePath '$env:TEMP\$($launch.LogName)'
"@

    $tempScript = "$env:TEMP\hermes-remote-start-$Name.ps1"
    Set-Content -Path $tempScript -Value $startScript -Encoding UTF8

    try {
        $proc = Start-Process -FilePath "pwsh.exe" `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tempScript`"" `
            -PassThru -ErrorAction Stop

        Write-Log "Started $Name (PID: $($proc.Id), port: $port)" "SUCCESS"
        return $true
    } catch {
        Write-Log "Failed to start $Name`: $_" "ERROR"
        return $false
    }
}

function Wait-PortListening {
    <#
    .SYNOPSIS
        Block until the target port is in LISTEN, or timeout.

    .DESCRIPTION
        Polls Test-PortListening with exponential backoff (capped at 5s).
        Required because Start-Process returns as soon as pwsh.exe is up -- the
        embedded python child binds A2A_PORT only after config + skill load.
        Without this wait, the next sequential restart races TIME_WAIT or
        sees EADDRINUSE.
    #>
    param(
        [int]$Port,
        [int]$MaxSeconds = 30
    )
    $elapsed = 0
    $delay = 1
    while ($elapsed -lt $MaxSeconds) {
        if (Test-PortListening -Port $Port) {
            return $true
        }
        Start-Sleep -Seconds $delay
        $elapsed += $delay
        if ($delay -lt 5) { $delay++ }
    }
    return $false
}

# === Job Object Detach (PR-3, incident 2026-10-15) ===
#
# Why this exists:
# When `gateway-restart-remote.ps1` is invoked from inside a Job Object (the
# `hermes update` parent creates Job Object #48820 on launch), every child
# process it spawns -- including the `pwsh.exe` + `hermes.exe` we hand off to
# in Start-Gateway -- inherits that Job Object by default. When the parent
# process exits (e.g. `hermes update` finishes or aborts), Windows tears
# down the Job Object and ALL assigned processes die, regardless of any
# Start-Process -WindowStyle Hidden / detach-style flags we set.
#
# We can't fix this in `hermes update` itself without an upstream change.
# The canonical Windows workaround for Job Object inheritance is to schedule
# the child in a process tree that the Job Object cannot see. The Windows
# Task Scheduler service (`schtasks.exe`) runs as a system service outside
# any user-mode Job Object, and tasks it spawns are placed in a fresh
# process tree unassigned to the parent's Job Object.
#
# Implementation:
# - `Restart-GatewayDetached`: creates an ONCE schtasks entry that fires
#   1 minute in the future and re-invokes this script with the same -Profile
#   + -Force. Returns immediately.
# - `-DetachViaScheduledTask` switch on `Restart-Gateway` (default: $true):
#   when set, uses the detached path. When $false, runs inline (legacy).
# - `Remove-StaleRestartTasks`: cleanup function that deletes
#   `hermes-restart-*` scheduled tasks older than 1 hour, called from MAIN.

function Restart-GatewayDetached {
    <#
    .SYNOPSIS
        Schedule a gateway restart 1 minute in the future via schtasks.

    .DESCRIPTION
        Creates a `hermes-restart-<profile>-<timestamp>` ONCE scheduled task
        that runs this script with `-Profile <profile> -Force` after a 1
        minute delay. The Task Scheduler service runs outside any user-mode
        Job Object, so the spawned gateway is NOT assigned to Job Object
        #48820 (or whatever Job Object the caller was inside) and survives
        the parent process exit.

        This is the canonical Windows workaround for Job Object inheritance
        when the upstream tool refuses to drop the Job Object (in our case,
        `hermes update` -- long-term fix requires upstream modification,
        NOT in scope of CEO-approved triet-de fix).
    #>
    param(
        [Parameter(Mandatory=$true)][string]$Profile
    )

    # Compute start time = now + 1 minute, formatted HH:MM (schtasks /ST)
    $startTime = (Get-Date).AddMinutes(1).ToString("HH:mm")
    # Timestamp suffix for the task name (sortable + unique)
    $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $taskName = "hermes-restart-$Profile-$stamp"

    # Build the command line the task will run. Use this script's path; if
    # the caller was dot-sourced or otherwise invoked without a known path,
    # fall back to the canonical AppData runtime copy.
    #
    # t_pm-gateway-abi (2026-11-15): `$MyInvocation.MyCommand` can be $null when
    # this function is reached through a caller that ran under
    # `Set-StrictMode -Version Latest`; reading `.Path` off $null then throws
    # `PropertyNotFoundStrict` and the restart NEVER starts the gateway (the
    # profile is stopped first, so the lane ends up DOWN). Read it defensively.
    $scriptPath = $null
    try {
        $scriptPath = $MyInvocation.MyCommand.Path
    } catch {
        $scriptPath = $null
    }
    if (-not $scriptPath -or -not (Test-Path $scriptPath)) {
        $scriptPath = "$HermesHome\gateway-restart-remote.ps1"
    }

    # schtasks /TR requires the command line as a single string. Quote the
        # script path because it contains spaces under AppData\Local.
        # t_f8de9a75: the scheduled re-invocation MUST ask for the inline path.
        # Without -Inline the spawned task re-enters this same deferred branch and
        # schedules ANOTHER identical task 1 minute later, forever: the gateway is
        # stopped and never started, one orphan task accumulates per minute, and the
        # caller is still told [OK]. -Inline makes the spawned task do the real
        # restart (Start-Gateway + port wait + health verification).
        #
        # Windowless: the scheduled task runs in the CEO's interactive desktop
        # session, so a console-subsystem child would RAISE a console window on
        # the desktop and steal the keyboard focus. -WindowStyle Hidden prevents
        # the console window from being shown. The absolute $PWSH_EXE path keeps
        # the action independent of the task's PATH.
        #
        # Self-cleanup: -CleanupTaskName carries the task's OWN name so the task
        # deletes itself when it finishes (see the finally block around MAIN).
        $taskArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Profile $Profile -Force -Inline -CleanupTaskName $taskName"
        $taskCmd  = "`"$PWSH_EXE`" $taskArgs"

        Write-Log "Scheduling detached restart: $taskName @ $startTime" "INFO"

        # WHY Register-ScheduledTask -Action and NOT `schtasks /Create /TR`:
        #   schtasks stores the whole action line in ONE /TR string and HARD-LIMITS
        #   it to 261 characters ("ERROR: Value for '/TR' option cannot be more
        #   than 261 character(s)"). The action line below is longer than that for
        #   the long profile names (e.g. 'security-reviewer-bangioi'), so schtasks
        #   would REJECT the registration and the deferred restart would silently
        #   never happen. Register-ScheduledTask stores Command and Arguments as
        #   SEPARATE fields, so there is no length cliff and no /TR tokenization.
        #   It is also the mechanism the live host already uses for the sibling
        #   hidden tasks (HermesKanbanNotifyRepair / HermesKanbanPrOrphanAudit).
        #   Principal: Interactive + Limited == the interactive-token principal the
        #   schtasks-created task had, so the task still runs in the user's session.
        try {
            $action = New-ScheduledTaskAction -Execute $PWSH_EXE -Argument $taskArgs
            $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
            Write-Log "Scheduled task created: $taskName (fires at $startTime)" "SUCCESS"
            Write-Log "  action: $taskCmd" "INFO"
            return $true
        } catch {
            Write-Log "Register-ScheduledTask failed: $_" "ERROR"
            return $false
        }
    }

function Remove-StaleRestartTasks {
    <#
    .SYNOPSIS
        Delete hermes-restart-* scheduled tasks older than 1 hour.

    .DESCRIPTION
        Walks all scheduled tasks whose name starts with `hermes-restart-`,
        parses the `-<timestamp>` suffix to determine age, and deletes
        entries older than 1 hour. Safe to call multiple times (no-op if
        nothing matches). Defends against task name accumulation if many
        restarts are scheduled without the parent process cleaning up.
    #>
    param([int]$OlderThanMinutes = 60)

    Write-Log "Cleaning up hermes-restart-* scheduled tasks older than ${OlderThanMinutes}m" "INFO"

    try {
        # Query all task names. schtasks /Query /FO LIST /V is too verbose.
        #
        # WHY /FO CSV and NOT /FO TABLE (the 207-orphan bug):
        #   /FO TABLE renders the name in a FIXED-WIDTH column and TRUNCATES it
        #   at 40 characters. 'hermes-restart-senior-techlead-bangioi-20261106-124814'
        #   came back as 'hermes-restart-senior-techlead-bangi', so the trailing
        #   '-(\d{8}-\d{6})$' timestamp regex NEVER matched and the task was
        #   never deleted -- 207 orphans piled up on the host.
        #   /FO CSV keeps every field intact; the task name is the FIRST quoted
        #   field of each row.
        $out = & schtasks.exe /Query /FO CSV /NH 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Log "schtasks /Query failed (exit $LASTEXITCODE): $out" "WARN"
            return
        }
        $lines = @($out | Where-Object { $_ -match '^\s*"hermes-restart-' -or $_ -match '^\s*"\\hermes-restart-' })
        if ($lines.Count -eq 0) {
            Write-Log "No hermes-restart-* tasks to clean" "INFO"
            return
        }
        $now = Get-Date
        $removed = 0
        foreach ($line in $lines) {
            # The task name is the FIRST quoted CSV field. Root-folder tasks
            # are prefixed with a backslash in the CSV output ('"\CI Health
            # Watchdog"'), so strip it before matching/deleting.
            if ($line -notmatch '^\s*"([^"]+)"') { continue }
            $name = $Matches[1] -replace '^\\', ''
            # Parse the timestamp suffix: hermes-restart-<profile>-yyyyMMdd-HHmmss
            if ($name -match '-(\d{8}-\d{6})$') {
                $stamp = $Matches[1]
                try {
                    $created = [DateTime]::ParseExact($stamp, "yyyyMMdd-HHmmss", $null)
                } catch {
                    Write-Log "Could not parse timestamp on $name, skipping" "WARN"
                    continue
                }
                $age = $now - $created
                if ($age.TotalMinutes -gt $OlderThanMinutes) {
                    & schtasks.exe /Delete /TN $name /F 2>&1 | Out-Null
                    if ($LASTEXITCODE -eq 0) {
                        $removed++
                        Write-Log "Removed stale task: $name (age $([int]$age.TotalMinutes)m)" "INFO"
                    } else {
                        Write-Log "Failed to remove $name (exit $LASTEXITCODE)" "WARN"
                    }
                }
            }
        }
        Write-Log "Cleanup complete: removed $removed stale task(s)" "INFO"
    } catch {
        Write-Log "Remove-StaleRestartTasks threw: $_" "WARN"
    }
}

function Write-RestartLease {
    <#
    .SYNOPSIS
        Tell every client on this host that this lane is being restarted, so none of them
        resurrects it.
    .DESCRIPTION
        Measured on this host 2026-11-26 06:16-06:19 (kanban t_784ed637, Defect 2 P1): while the
        restarted ROOT lane was DOWN, nine surviving `kanban_worker_client` processes replayed the
        lane argv recorded in `gateway_state.json` within 40 s. One child (pid 24004) took
        `gateway.lock` and never published a host record; the other eight exited with
        "Another gateway instance is already running (PID 24004 ...) and did not publish a host
        record this process could attach to", the respawn-storm backoff fired at 11 restarts in
        120 s, and :9920 stayed unbound for another 11 minutes.

        The lease is a FILE, not a lock: `hermes_cli/gateway_runtime_start.py::_restart_lease`
        reads `<lane home>\.gateway-restart-in-progress.json` before it spawns a successor and
        refuses while the lease is live. It is deliberately self-expiring - a restarter that dies
        or hangs (exactly what happened here) cannot hold a lane down past `expires_at`, which is
        what separates this from the `--force` style markers that need a human to clear them.

        The schema is consumed by python, so it stays flat and boring:
            { pid, started_at (epoch s), expires_at (epoch s), reason, lane }
    #>
    param(
        [Parameter(Mandatory = $true)][string]$LaneHome,
        [int]$Seconds = 0,
        [string]$Reason = 'restart-remote'
    )

    # Window = the stop's own grace + the start/verify budget + headroom, capped. python's
    # `_RESTART_LEASE_MAX_SECONDS` ignores anything longer, so exceeding the cap would silently
    # disable the lease - stay inside it.
    if ($Seconds -le 0) {
        $Seconds = (Get-PlannedStopGraceSeconds -LaneHome $LaneHome) + $MaxWaitSeconds + 120
    }
    if ($Seconds -gt 1800) { $Seconds = 1800 }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $lease = [ordered]@{
        pid        = $PID
        started_at = $now
        expires_at = $now + $Seconds
        reason     = $Reason
        lane       = (Split-Path -Leaf $LaneHome)
    }
    $path = [System.IO.Path]::Combine($LaneHome, '.gateway-restart-in-progress.json')
    try {
        [System.IO.File]::WriteAllText(
            $path,
            ($lease | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        # Never fatal: the lease only avoids a storm, it is not a safety property. A lane that
        # cannot write it is restarted exactly as before this change.
        Write-Log "RESTART-LEASE: cannot write ${path} ($($_.Exception.Message)) - clients are NOT told this lane is restarting" "WARN"
        return $false
    }
    Write-Log "RESTART-LEASE: $($lease.lane) is restarting for the next ${Seconds}s (advertised to clients so none of them resurrects it)" "INFO"
    return $true
}

function Remove-RestartLease {
    <#
    .SYNOPSIS
        Withdraw the restart lease (the lane is up again, or this run gave up on it).
    .DESCRIPTION
        Removal is advisory: the file expires on its own, so a crash here can only shorten the
        window a client stays out of, never extend it.
    #>
    param([Parameter(Mandatory = $true)][string]$LaneHome)

    $path = [System.IO.Path]::Combine($LaneHome, '.gateway-restart-in-progress.json')
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function Restart-Gateway {
    <#
    .SYNOPSIS
        Per-profile restart wrapper with optional Job Object detach.

    .DESCRIPTION
        Encapsulates the original MAIN-loop sequence (Stop-Gateway, wait for
        port, Start-Gateway, wait for LISTEN, verify health) and adds a
        `-DetachViaScheduledTask` switch (default $true) that defers the
        actual restart by 1 minute via a schtasks entry. When the caller
        is running inside a Job Object (the `hermes update` parent case),
        the detached path puts the new gateway in a fresh process tree
        that is NOT assigned to the caller's Job Object, so it survives
        the caller's exit.
    #>
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [switch]$Force,
        [switch]$DetachViaScheduledTask = $true,
        [int]$MaxWaitSeconds = 60
    )

    $port = Get-ProfilePort -Name $Name

    # Stop-then-refuse hazard (review 5931797532): every path below stops the live
    # gateway BEFORE Start-Gateway re-checks the launch spec. Validate THIS lane's
    # spec first, so a spec defect can never take a lane down and then refuse to
    # bring it back up.
    $null = Assert-TargetLaunchSpecsPinned -Names @($Name)

    # Stop-then-NEVER-start hazard (t_784ed637 P0): Start-Gateway resolves the
    # ABI-matched launcher at its point of use - i.e. AFTER this wrapper has already
    # stopped the lane. Resolve it HERE, while the lane is still UP, so a launcher that
    # cannot be proven compatible is a refused restart instead of a dead lane. The
    # answer is cached, so Start-Gateway never re-probes after the stop.
    $null = Assert-LauncherResolvable -Names @($Name)

    if ($DetachViaScheduledTask) {
        # Detached path: schedule the actual restart for 1 minute in the
        # future, return immediately. This is the Job Object workaround --
        # see the comment block above Restart-GatewayDetached for the
        # mechanism explanation.
        Write-Log "=== Restarting $Name (detached via schtasks) ===" "RESTART"
        # t_784ed637 (Defect 2): advertise the restart BEFORE anything is stopped, so a client
        # that finds the lane absent during the next minute cannot resurrect it. The deferred
        # child re-writes the lease for its own run and withdraws it when the lane is back; this
        # one therefore covers defer + start + verify and is NOT removed here.
        $null = Write-RestartLease -LaneHome (Get-LaneHome -Name $Name) -Reason 'restart-remote-detached'
        # Stop inline so the OLD gateway is gone before the new one binds;
        # otherwise the schtasks-spawned child would EADDRINUSE on its
        # 1-minute-future start. (Detach only protects the NEW process tree.)
        # t_3a13db7b: abort the schedule when the stop did not actually vacate the home.
        # Scheduling the start anyway is the concurrent-start bug: the deferred task would
        # spawn a second gateway on a home whose owner is still alive.
        $stopped = Stop-Gateway -Name $Name -Hard:$Force
        if (-not $stopped) {
            Write-Log "REFUSING to schedule restart of ${Name}: the previous owner did not exit" "ERROR"
            Remove-RestartLease -LaneHome (Get-LaneHome -Name $Name)
            return @{
                Success = $false
                WaitSeconds = 0
                Detached = $true
                Error = "Previous owner still owns the HERMES_HOME -- not spawning a second gateway"
            }
        }
        $scheduled = Restart-GatewayDetached -Profile $Name
        if (-not $scheduled) {
            # Nothing is coming: withdraw the claim so a client can start the lane again.
            Remove-RestartLease -LaneHome (Get-LaneHome -Name $Name)
        }
        return @{
            Success = $scheduled
            WaitSeconds = 0
            Detached = $true
            Error = if (-not $scheduled) { "schtasks scheduling failed" } else { $null }
        }
    }

    # Legacy inline path (preserved for callers that explicitly opt out,
    # e.g. an interactive admin session running outside any Job Object).
    Write-Log "=== Restarting $Name (inline) ===" "RESTART"

    # t_784ed637 (Defect 2): claim the lane for this restart before the stop, so the clients that
    # find it absent mid-restart wait instead of spawning 9 competing daemons. The lease expires on
    # its own if this process dies, so it can only ever shorten the window a client stays out of.
    $laneHome = Get-LaneHome -Name $Name
    $leaseSeconds = (Get-PlannedStopGraceSeconds -LaneHome $laneHome) + $MaxWaitSeconds + 120
    $null = Write-RestartLease -LaneHome $laneHome -Seconds $leaseSeconds -Reason 'restart-remote-inline'
    try {
    # t_3a13db7b (AC2): stop-then-start, never concurrent-start.
    $stopped = Stop-Gateway -Name $Name -Hard:$Force
    if (-not $stopped) {
        Write-Log "REFUSING to start ${Name}: the previous owner did not exit" "ERROR"
        return @{
            Success = $false; WaitSeconds = 0; Detached = $false
            Error = "Previous owner still owns the HERMES_HOME -- not spawning a second gateway"
        }
    }

    # Wait for port to free (max 20s; TIME_WAIT can persist 30-120s on Windows)
    $waited = 0
    while ($waited -lt 20) {
        if (-not (Test-PortListening -Port $port)) { break }
        Start-Sleep -Seconds 1
        $waited++
    }
    if ($waited -ge 20) {
        Write-Log "Port $port still in TIME_WAIT for $Name after 20s -- proceeding anyway" "WARN"
    }

    $started = Start-Gateway -Name $Name
    if (-not $started) {
        return @{ Success = $false; WaitSeconds = 0; Detached = $false; Error = "Start failed" }
    }

    $bound = Wait-PortListening -Port $port -MaxSeconds 20
    if (-not $bound) {
        Write-Log "$Name started but port $port did not enter LISTEN within 20s" "WARN"
    }

    # Verify health
    $elapsed = 0
    while ($elapsed -lt $MaxWaitSeconds) {
        Start-Sleep -Seconds 5
        $elapsed += 5
        if (Test-GatewayHealthy -Name $Name) {
            Write-Log "$Name is HEALTHY after ${elapsed}s (port $port)" "SUCCESS"
            return @{ Success = $true; WaitSeconds = $elapsed; Detached = $false; Error = $null }
        }
    }
    return @{ Success = $false; WaitSeconds = $elapsed; Detached = $false; Error = "Timeout after ${MaxWaitSeconds}s" }
    } finally {
        # Withdraw the claim on EVERY exit (healthy, failed, refused): leaving it behind would keep
        # clients out of a lane that this run has finished with, until the lease expires.
        Remove-RestartLease -LaneHome $laneHome
    }
}

# === MAIN ===
# Skip MAIN when dot-sourced from Pester tests or library loaders -- only run
# when invoked directly via CLI ($MyInvocation.InvocationName will be the
# script path, not '.').
if ($MyInvocation.InvocationName -ne '.') {
# The whole MAIN body runs inside a try/finally so the deferred one-shot
# task deletes ITSELF when it finishes. PowerShell unwinds through `finally`
# on `exit`, so the cleanup runs on the success path (exit 0) AND the
# failure path (exit 1) -- a task that crashed mid-restart would otherwise
# linger forever (207 such orphans piled up on the host before this).
try {
# t_e4882d04: fail-loud parity self-test BEFORE touching anything. If the
# supervisor's $Profiles and this tool's profile set have drifted, abort now
# instead of silently skipping non-restartable lanes mid-restart.
Assert-SupervisorProfileParity | Out-Null
# PR-3 (incident 2026-10-15): best-effort stale-task cleanup before the run.
# Safe to call multiple times; no-op if nothing to clean.
Remove-StaleRestartTasks -OlderThanMinutes 60

$targets = Get-TargetProfiles -ProfileName $Profile

# Same stop-then-refuse hazard at the RUN level (review 5931797532): `-Profile all`
# must refuse the WHOLE run before the first stop, never restart lanes 1..N and then
# abort on lane N+1 with that lane already DOWN.
$null = Assert-TargetLaunchSpecsPinned -Names $targets

# Same shape for the LAUNCHER (t_784ed637 P0): the ABI probe used to run after the
# stop and, on 2026-11-26, hung >7m50s and left the ROOT lane down with no dispatcher.
# Resolving it here - once, for the whole wave, while every lane is UP - turns an
# unresolvable launcher into a refused run instead of a fleet-wide outage. Start-Gateway
# reuses this cached answer, so nothing is probed again after the first Stop-Gateway.
try {
    $null = Assert-LauncherResolvable -Names $targets
} catch {
    Write-Log $_.Exception.Message "ERROR"
    foreach ($name in $targets) {
        Write-Host ("  [FAIL] {0} : NOT STOPPED - {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    }
    exit 1
}

$results = @{}

foreach ($name in $targets) {
    # PR-3 (incident 2026-10-15): delegate to Restart-Gateway wrapper. The
    # wrapper applies -DetachViaScheduledTask (default $true) so the spawned
    # gateway lives in a fresh process tree outside the caller's Job Object.
    # When -WaitVerify is $false, the caller explicitly opts out of waiting
    # for health verification -- the wrapper honors that and still runs the
    # full Stop+Start sequence in detached mode.
    #
    # P0 (kanban t_f8de9a75): the switch is bound with the `:` form, never
    # positionally. `-DetachViaScheduledTask $true` binds $true POSITIONALLY and
    # a [switch] has no positional slot, so PowerShell raises "A positional
    # parameter cannot be found that accepts argument 'True'",
    # Restart-Gateway is never entered, and EVERY profile restart fails.
    # The `:` form makes the intent explicit and lets -Inline force the inline
    # path on the deferred task's own re-invocation.
    #
    # AC3 (t_4139ff93, 2026-11-04): wrap the call in try/catch to catch
    # PositionalParameterNotFound and any parameter-binding error. If the
    # function is never entered, mark the profile as NOT EXECUTED with the
    # FullyQualifiedErrorId. An empty .Error must never reach the summary as
    # a bare "FAILED ()".
    try {
        $r = Restart-Gateway -Name $name -Force:$Force -DetachViaScheduledTask:(-not $Inline) -MaxWaitSeconds $MaxWaitSeconds
        if (-not $WaitVerify -and $r.Detached) {
            # Caller asked not to wait -- the wrapper already detached; report success
            # even though we never saw the new process bind (it will, in 1 minute).
            $results[$name] = @{ Success = $r.Success; WaitSeconds = 0; Detached = $true; Error = $r.Error }
        } else {
            $results[$name] = $r
        }
    } catch {
        # AC3: fail loudly on parameter-binding errors (PositionalParameterNotFound)
        # or any exception that prevents Restart-Gateway from entering.
        $fqei = $null
        if ($_.FullyQualifiedErrorId) {
            $fqei = $_.FullyQualifiedErrorId
        }
        Write-Host ""
        Write-Host "===========================================" -ForegroundColor Red
        Write-Host "  ERROR: $name NOT EXECUTED" -ForegroundColor Red
        Write-Host "===========================================" -ForegroundColor Red
        Write-Host "  Exception: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  FullyQualifiedErrorId: $fqei" -ForegroundColor Red
        Write-Host "  The Restart-Gateway function was never entered." -ForegroundColor Red
        Write-Host "  This is typically a parameter-binding error (e.g., a switch" -ForegroundColor Red
        Write-Host "  bound positionally instead of with the ':' form)." -ForegroundColor Red
        Write-Host "===========================================" -ForegroundColor Red
        Write-Host ""
        $results[$name] = @{ Success = $false; WaitSeconds = 0; Detached = $false; Error = "NOT EXECUTED ($fqei): $($_.Exception.Message)" }
    }
}

# Summary
Write-Host ""
Write-Host "==========================================="
Write-Host "       RESTART SUMMARY"
Write-Host "==========================================="

$allSuccess = $true
foreach ($name in $targets) {
    $r = $results[$name]
    if ($r.Success) {
        Write-Host "  [OK] $name : OK ($($r.WaitSeconds)s)" -ForegroundColor Green
    } else {
        # AC3: never emit a bare "FAILED ()" — if .Error is null/empty,
        # surface a diagnostic instead.
        $errText = $r.Error
        if (-not $errText -or $errText.Trim() -eq '') {
            $errText = "UNKNOWN (no error text captured — check the NOT EXECUTED banner above)"
        }
        Write-Host "  [FAIL] $name : FAILED ($errText)" -ForegroundColor Red
        $allSuccess = $false
    }
}

Write-Host "==========================================="

if ($allSuccess) {
    Write-Log "All gateways restarted successfully!" "SUCCESS"
    exit 0
} else {
    Write-Log "Some gateways failed to restart" "ERROR"
    exit 1
}
} # end try
finally {
    # Self-cleanup: the deferred one-shot task removes ITSELF. The task name
    # is passed in via -CleanupTaskName by Restart-GatewayDetached. Without
    # this, every scheduled restart left a task behind forever (207 orphans
    # accumulated on the host before this fix).
    #
    # PowerShell unwinds through `finally` on `exit`, so this runs on BOTH the
    # success path (exit 0 above) and the failure path (exit 1 above).
    if ($CleanupTaskName) {
        try {
            & schtasks.exe /Delete /TN $CleanupTaskName /F 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Log "Cleaned up deferred restart task: $CleanupTaskName" "INFO"
            } else {
                Write-Log "Could not delete deferred restart task '$CleanupTaskName' (exit $LASTEXITCODE)" "WARN"
            }
        } catch {
            Write-Log "Cleanup of '$CleanupTaskName' threw: $_" "WARN"
        }
    }
}
} # end if ($MyInvocation.InvocationName -ne '.')

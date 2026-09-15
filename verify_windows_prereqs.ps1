$ErrorActionPreference = 'Stop'
$checks = @(
    @{Name='Python'; Command='python'; Args='--version'},
    @{Name='Node.js'; Command='node'; Args='--version'},
    @{Name='npm'; Command='npm'; Args='--version'},
    @{Name='Rust/Cargo'; Command='cargo'; Args='--version'},
    @{Name='Rustc'; Command='rustc'; Args='--version'}
)
$failed = $false
foreach ($c in $checks) {
    $cmd = Get-Command $c.Command -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "[MISSING] $($c.Name)" -ForegroundColor Red
        $failed = $true
        continue
    }
    $v = & $c.Command $c.Args 2>$null
    Write-Host "[OK] $($c.Name): $v" -ForegroundColor Green
}
if ($failed) {
    throw 'One or more build-time prerequisites are missing. Use GitHub Actions if you do not want build tools installed locally.'
}
Write-Host 'Build prerequisites look available.' -ForegroundColor Cyan

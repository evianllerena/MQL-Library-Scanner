param(
  [Parameter(Mandatory=$true)][string]$SourceFolder
)
$ErrorActionPreference='Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Db = Join-Path $env:TEMP 'mql_indicator_library_engine_test.sqlite3'
if(Test-Path $Db){Remove-Item $Db -Force}
python "$Root\engine\engine.py" scan --db $Db --source $SourceFolder
python "$Root\engine\engine.py" stats --db $Db
Write-Host "Test database: $Db"

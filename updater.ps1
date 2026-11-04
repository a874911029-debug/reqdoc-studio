# ReqDoc updater-only bootstrap. It never replaces the GUI or runtime.
param(
  [Parameter(Mandatory=$true)][string]$InstallDir,
  [Parameter(Mandatory=$true)][string]$PackagePath,
  [Parameter(Mandatory=$true)][int]$AppPid,
  [Parameter(Mandatory=$true)][string]$Sha256
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$logDir=Join-Path $env:LOCALAPPDATA 'ReqDoc\updates'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log=Join-Path $logDir ('update-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.log')
function Log([string]$message) {
  (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')+' '+$message | Out-File -LiteralPath $log -Append -Encoding UTF8
}
$work=Join-Path $env:TEMP ('reqdoc-install-'+[guid]::NewGuid().ToString('N'))
$stage=Join-Path $work 'stage'
$backup=Join-Path $work 'backup'
$overwritten=New-Object 'System.Collections.Generic.List[string]'
$required=@('_internal\reqdoc_updater.py','updater.ps1','version.json')
$allowed=@('_internal\reqdoc_updater.py','updater.ps1','version.json')
try {
  Log 'Updater-only bootstrap started'
  $InstallDir=[System.IO.Path]::GetFullPath($InstallDir)
  $PackagePath=[System.IO.Path]::GetFullPath($PackagePath)
  if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) { throw 'ZIP file not found' }
  if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'reqdoc-gui.exe') -PathType Leaf)) {
    throw 'Invalid installation directory'
  }
  foreach ($rel in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $rel) -PathType Leaf)) {
      throw ('This installation is missing a bootstrap target: '+$rel)
    }
  }
  $actual=(Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Sha256.ToLowerInvariant()) { throw 'ZIP SHA-256 mismatch' }

  # Wait for the old GUI to release its files; never force-kill it.
  for ($i=0; $i -lt 120; $i++) {
    if (-not (Get-Process -Id $AppPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
  }
  if (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) { throw 'ReqDoc is still running' }

  New-Item -ItemType Directory -Force -Path $stage,$backup | Out-Null
  Expand-Archive -LiteralPath $PackagePath -DestinationPath $stage -Force

  $metadata=Get-Content -LiteralPath (Join-Path $stage 'version.json') -Raw | ConvertFrom-Json
  if ($metadata.name -ne 'ReqDoc' -or $metadata.version -notmatch '^\d+\.\d+\.\d+$' -or
      $metadata.package_type -ne 'updater-bootstrap') {
    throw 'Invalid update version metadata'
  }

  $stageFiles=Get-ChildItem -LiteralPath $stage -File -Recurse -Force
  if ($stageFiles.Count -ne 3) { throw 'Updater patch must contain exactly three files' }
  foreach ($file in $stageFiles) {
    $rel=$file.FullName.Substring($stage.Length).TrimStart('\','/').Replace('/','\')
    if ($allowed -notcontains $rel) { throw ('Disallowed update path: '+$rel) }
  }

  Log ('Validated '+$stageFiles.Count+' updater files')
  foreach ($file in $stageFiles) {
    $rel=$file.FullName.Substring($stage.Length).TrimStart('\','/').Replace('/','\')
    $dest=Join-Path $InstallDir $rel
    $saved=Join-Path $backup $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $saved -Parent) | Out-Null
    Copy-Item -LiteralPath $dest -Destination $saved -Force
    $overwritten.Add($rel)
  }

  foreach ($file in $stageFiles) {
    $rel=$file.FullName.Substring($stage.Length).TrimStart('\','/').Replace('/','\')
    $dest=Join-Path $InstallDir $rel
    Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
  }

  # Verify that the app can start before declaring the migration successful.
  $process=Start-Process -FilePath (Join-Path $InstallDir 'reqdoc-gui.exe') -WorkingDirectory $InstallDir -PassThru
  Start-Sleep -Seconds 2
  if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
    throw 'ReqDoc exited immediately after updater migration'
  }

  $archiveDir=Join-Path $env:LOCALAPPDATA 'ReqDoc\program-backups'
  New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
  $savedDir=Join-Path $archiveDir ('updater-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
  try {
    Copy-Item -LiteralPath $backup -Destination $savedDir -Recurse -Force
    Log ('Updater migration installed; old updater backup at '+$savedDir)
  } catch {
    Log ('Updater migration installed; backup copy failed: '+$_.Exception.Message)
  }
  Log 'Application relaunched successfully'
} catch {
  Log ('FAILED: '+$_.Exception.Message)
  foreach ($rel in $overwritten) {
    $saved=Join-Path $backup $rel
    $dest=Join-Path $InstallDir $rel
    if (Test-Path -LiteralPath $saved -PathType Leaf) {
      try { Copy-Item -LiteralPath $saved -Destination $dest -Force }
      catch { Log ('ROLLBACK FAILED: '+$rel) }
    }
  }
  Log 'Rollback attempted; see details above'
  try {
    if (-not (Get-Process -Name 'reqdoc-gui' -ErrorAction SilentlyContinue) -and
        (Test-Path -LiteralPath (Join-Path $InstallDir 'reqdoc-gui.exe'))) {
      Start-Process -FilePath (Join-Path $InstallDir 'reqdoc-gui.exe') -WorkingDirectory $InstallDir
    }
  } catch { Log ('Old application restart failed: '+$_.Exception.Message) }
  exit 1
}

# Preparation only: NOT executed against production. No passwords in arguments/logs.
# Configure a libpq service with verified TLS and an operator-owned pgpass first.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_-]+$')][string]$PgService,
  [Parameter(Mandatory=$true)][string]$BackupDirectory
)
$ErrorActionPreference='Stop'
$dumpTool=(Get-Command pg_dump -ErrorAction Stop).Source
$restoreTool=(Get-Command pg_restore -ErrorAction Stop).Source
$rolesTool=(Get-Command pg_dumpall -ErrorAction Stop).Source
$taskBackupRoot=[System.IO.Path]::GetFullPath($BackupDirectory)
if (Test-Path -LiteralPath $taskBackupRoot) { throw 'Choose a new empty backup directory; existing paths are never overwritten.' }
New-Item -ItemType Directory -Path $taskBackupRoot | Out-Null
$taskDump=Join-Path $taskBackupRoot 'database.dump'
$taskRoles=Join-Path $taskBackupRoot 'roles.sql'
& $dumpTool --dbname="service=$PgService" --format=custom --file=$taskDump --lock-wait-timeout=15000
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed. Preserve partial files for diagnosis; do not deploy.' }
# Password hashes are excluded. Managed Supabase roles must not be blindly restored.
& $rolesTool --dbname="service=$PgService" --roles-only --no-role-passwords --file=$taskRoles
if ($LASTEXITCODE -ne 0) { throw 'Role metadata backup failed. Do not call the backup complete.' }
& $restoreTool --list $taskDump | Set-Content -LiteralPath (Join-Path $taskBackupRoot 'contents.txt') -Encoding UTF8
if ($LASTEXITCODE -ne 0) { throw 'Archive verification failed.' }
Get-FileHash -Algorithm SHA256 -LiteralPath $taskDump,$taskRoles | Export-Csv -NoTypeInformation -LiteralPath (Join-Path $taskBackupRoot 'sha256.csv')
Write-Output 'Archive created, NOT restore-tested. Secure/encrypt it; separately back up Storage objects, IKUGO disk and service configuration. Restore only into a new isolated compatible Supabase project; reconcile managed roles/extensions and row counts before cutover.'

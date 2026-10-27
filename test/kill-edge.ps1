param([Parameter(Mandatory=$true)][string]$Match = 'xbprofile')
$procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
    Where-Object { $_.CommandLine -like "*$Match*" }
if (-not $procs) { Write-Output "no msedge process matching '$Match'"; exit 0 }
foreach ($p in $procs) {
    Write-Output ("kill {0}" -f $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output ("killed {0} process(es)" -f $procs.Count)

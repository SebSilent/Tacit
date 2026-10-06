# Restart the Tacit server on this host.
# 1) find the python process that belongs to Tacit (backend/uvicorn/run reference in its command line)
# 2) stop it, 3) start run.bat detached and hidden so it survives this console.
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='uvicorn.exe'"
$tacit = $procs | Where-Object { $_.CommandLine -match 'Tacit|backend\.server|uvicorn' -and $_.CommandLine -notmatch 'winllm' }
foreach ($p in $tacit) {
    Write-Output ("stopping " + $p.ProcessId + " : " + $p.CommandLine)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
Start-Process -FilePath "C:\Projects\Tacit\run.bat" -WorkingDirectory "C:\Projects\Tacit" -WindowStyle Minimized
Start-Sleep -Seconds 6
$after = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='uvicorn.exe'"
$after | ForEach-Object { Write-Output ("live " + $_.ProcessId + " : " + ($_.CommandLine.Substring(0, [Math]::Min(90, $_.CommandLine.Length)))) }
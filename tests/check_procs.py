import subprocess

ps_script = """
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" | ForEach-Object {
    if ($_.CommandLine -like '*zain-checker*') {
        Write-Output $_.CommandLine
    }
}
"""

res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True)
lines = [l for l in res.stdout.splitlines() if l.strip()]
print(f"Total active zain-checker processes: {len(lines)}")
for idx, l in enumerate(lines, 1):
    print(f"{idx}: {l[:150]}")

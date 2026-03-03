# Deploy bot folder to EC2 and restart inverse bot
# Usage: .\deploy.ps1   (edit $KeyPath if your EC2 key is different)

$ErrorActionPreference = "Continue"
$KeyPath = "C:\Users\yoven\.ssh\yoyo.pem"
$EC2 = "34.242.67.217"
$ProjectRoot = "C:\Users\yoven\Downloads\reneai landing\reneai-landing"
$BotPath = Join-Path $ProjectRoot "bot"

# Try ec2-user first (Amazon Linux), then ubuntu (Ubuntu AMI)
$Deployed = $false
foreach ($user in @("ec2-user", "ubuntu")) {
    Write-Host "Trying deploy as ${user}@${EC2}..."
    $null = & scp -i $KeyPath -o StrictHostKeyChecking=no -o ConnectTimeout=10 -r $BotPath "${user}@${EC2}:~/reneai-landing/" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Deploy OK."
        $Deployed = $true
        break
    }
}

if (-not $Deployed) {
    Write-Host "Deploy failed (SSH key may not match this EC2). Restarting inverse bot anyway..."
}

# Restart inverse bot via launcher
Write-Host "Restarting inverse bot..."
Invoke-WebRequest -Uri "http://${EC2}:8900/api/inverse/stop" -Method POST -UseBasicParsing | Out-Null
Start-Sleep -Seconds 2
$r = Invoke-WebRequest -Uri "http://${EC2}:8900/api/inverse/start" -Method POST -UseBasicParsing
$j = $r.Content | ConvertFrom-Json
if ($j.ok) { Write-Host "Inverse bot restarted." } else { Write-Host "Restart response: $($r.Content)" }

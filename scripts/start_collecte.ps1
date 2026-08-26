# Lance Docker Desktop puis (re)démarre les conteneurs de collecte (API + worker).
# Appelé par la tâche planifiée "Digiwise - Demarrage collecte sentiment" à l'ouverture de session.
#
# Pourquoi ce script plutôt que compter sur `restart: unless-stopped` seul :
# la politique de redémarrage ne joue que si Docker Desktop est déjà lancé.
# Sur Windows, Docker Desktop ne démarre pas tout seul au boot (AutoStart
# était à false) ; sans ce script, personne ne relance le moteur et donc
# personne ne relance les conteneurs.

$ErrorActionPreference = "Stop"
# Docker écrit sa progression normale sur stderr ; sans ceci, PowerShell 7 la
# traite comme une exception fatale même quand la commande réussit (code 0).
$PSNativeCommandUseErrorActionPreference = $false
$dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$projectDir = "D:\analyse_de_sentiment"
$logFile = Join-Path $projectDir "data\logs\autostart.log"

function Write-Log($message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] $message"
}

New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
Write-Log "Démarrage du script d'autostart"

if (-not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
    Write-Log "Docker Desktop non lancé -> démarrage"
    Start-Process $dockerDesktop
} else {
    Write-Log "Docker Desktop déjà lancé"
}

# Attend que le moteur Docker réponde (jusqu'à 5 minutes : le premier
# démarrage après un redémarrage complet de Windows peut être lent).
$deadline = (Get-Date).AddMinutes(5)
$ready = $false
do {
    Start-Sleep -Seconds 5
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true }
} until ($ready -or (Get-Date) -gt $deadline)

if (-not $ready) {
    Write-Log "ERREUR : le moteur Docker n'a pas répondu dans le délai imparti"
    exit 1
}

Write-Log "Moteur Docker prêt -> docker compose up -d"
Set-Location $projectDir
# docker écrit sa progression normale sur stderr ; sous $ErrorActionPreference
# = Stop, PowerShell la transforme en exception dès qu'elle est redirigée
# (*>>) — d'où le retour temporaire à Continue, le temps de cet appel.
$ErrorActionPreference = "Continue"
docker compose up -d 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -eq 0) {
    Write-Log "Conteneurs à jour"
} else {
    Write-Log "ERREUR : docker compose up -d a échoué (code $LASTEXITCODE)"
}

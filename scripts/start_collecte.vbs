' Lancé automatiquement à l'ouverture de session Windows (via le dossier
' Démarrage). Appelle start_collecte.ps1 en fenêtre cachée : PowerShell fait
' le vrai travail (démarrer Docker Desktop, attendre le moteur, relancer les
' conteneurs), ce .vbs sert seulement à éviter la fenêtre console qui
' s'afficherait sinon à chaque connexion.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\analyse_de_sentiment\scripts\start_collecte.ps1""", 0, False

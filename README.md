# OCR VPS Web App

Application web OCR pour traiter des PDF en lot, conserver l'arborescence des dossiers, et retourner des PDF OCR + compresses.

## Stack

- Backend: Python 3, Flask, Gunicorn
- Frontend: HTML/CSS/JS vanilla (single-page)
- OCR: `pdftoppm` + `tesseract` + `ghostscript`
- Auth: session Flask + `users.json` avec hash `bcrypt`

## Structure

```text
ocr_app/
├── app.py
├── worker.py
├── job_store.py
├── users.example.json
├── requirements.txt
├── templates/
│   ├── login.html
│   ├── index.html
│   └── exports.html
├── uploads/
├── outputs/
├── scripts/
│   ├── create_user.py
│   ├── install_server.sh
│   ├── check_dependencies.sh
│   ├── monitor_health.sh
│   └── setup_https_certbot.sh
├── .github/
│   └── workflows/
│       └── ci.yml
└── deploy/
    ├── ocr_app.service
    ├── ocr_worker.service
    ├── nginx_ocr_app.conf
    └── nginx_ocr_app_hardened.conf
```

## Installation locale

### 1) Dependances systeme (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara tesseract-ocr-eng poppler-utils ghostscript python3-pip
```

### 2) Dependances Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3) Config environnement

```bash
cp .env.example .env
```

Variables:

- `FLASK_SECRET_KEY`: cle secrete Flask
- `MAX_CONTENT_LENGTH_BYTES`: taille max requete upload
- `MAX_FILES_PER_JOB`: nombre max de PDF par job
- `MAX_WORKERS`: nombre de fichiers traites en parallele dans le worker
- `WORKER_POLL_SECONDS`: delai de polling de la file de jobs
- `JOB_TTL_SECONDS`: retention d'un job avant purge
- `CLEANUP_INTERVAL_SECONDS`: frequence de nettoyage
- `OCR_TIMEOUT_SECONDS`: timeout par commande OCR
- `OCR_MAX_RETRIES`: nombre de retries OCR
- `UPLOAD_RATE_LIMIT_WINDOW_SECONDS`: fenetre de rate limit upload
- `UPLOAD_RATE_LIMIT_MAX_REQUESTS`: nombre max d'uploads dans la fenetre
- `LOG_LEVEL`: niveau des logs (`INFO`, `WARNING`, ...)

### 4) Creer les utilisateurs

`users.json` n'est pas versionne (fichier local). Partir du template:

```bash
cp users.example.json users.json
python3 scripts/create_user.py equipe_scan "motdepasse_fort"
python3 scripts/create_user.py admin "autre_motdepasse_fort"
```

### 5) Demarrer en dev

```bash
python3 app.py
# terminal 2
python3 worker.py
```

Application disponible sur `http://localhost:5050`.

## Demarrage production

```bash
gunicorn -w 2 -b 0.0.0.0:5050 app:app
# worker separe
python3 worker.py
```

## Endpoints

- `GET/POST /login`
- `GET /logout`
- `GET /`
- `GET /health`
- `GET /metrics`
- `POST /upload`
- `GET /status/<job_id>`
- `GET /download/<job_id>/<path>`
- `GET /download_all/<job_id>`
- `GET /exports` (UI navigation des exports serveur)
- `GET /exports/api`
- `GET /exports/browse/<export_folder>`
- `GET /exports/file/<export_folder>/<path>`

## Pipeline OCR

1. `pdftoppm -r 300 input.pdf /tmp/job/pages/page`
2. `tesseract page-01.ppm page-01 -l fra+ara+eng --psm 1 pdf`
3. `gs -dBATCH -dNOPAUSE -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -sOutputFile=output.pdf page-01.pdf ...`
4. Nettoyage des fichiers temporaires

Traitement parallelise dans `worker.py` avec `ThreadPoolExecutor(max_workers=MAX_WORKERS)`.

## Export "fichiers traites"

A la fin d'un job, le worker copie les PDF reussis dans un dossier persistant:

- `fichiers_traites/<YYYYMMDD_HHMMSS>_<prefix-uuid>/...`

L'arborescence relative est conservee (ex: `RH/2024/contrat.pdf`).

En cas d'echecs partiels, un fichier `RAPPORT_ERREURS.txt` est ajoute dans ce dossier.

Le endpoint `/status/<job_id>` expose aussi `export.path` lorsque le dossier est pret.

Variable:

- `PROCESSED_DIR`: dossier racine des exports (defaut: `fichiers_traites/` a la racine du projet)

## Nettoyage automatique

Un thread de fond supprime toutes les 30 minutes les jobs vieux de plus de 8 heures dans:

- `uploads/<job_id>/`
- `outputs/<job_id>/`
- `outputs/<job_id>_all.zip`

## Deploy VPS (Nginx + systemd)

1. Copier le projet dans `/opt/ocr_app`
2. Creer et activer l'env Python
3. Installer requirements
4. Copier `deploy/ocr_app.service` vers `/etc/systemd/system/ocr_app.service`
5. Copier `deploy/ocr_worker.service` vers `/etc/systemd/system/ocr_worker.service`
6. Adapter `WorkingDirectory`, `EnvironmentFile`, et `ExecStart` dans les deux services
7. Activer services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ocr_app ocr_worker
sudo systemctl status ocr_app
sudo systemctl status ocr_worker
```

8. Copier `deploy/nginx_ocr_app_hardened.conf` dans `/etc/nginx/sites-available/ocr_app`
9. Activer site:

```bash
sudo ln -s /etc/nginx/sites-available/ocr_app /etc/nginx/sites-enabled/ocr_app
sudo nginx -t
sudo systemctl reload nginx
```

10. Configurer HTTPS certbot:

```bash
bash scripts/setup_https_certbot.sh your-domain.com admin@your-domain.com
```

## Sprints proposes

### Sprint 1 - MVP fonctionnel (fait)

- Authentification utilisateur via `users.json`
- Upload fichiers + dossiers
- OCR multi-langues et compression PDF
- Suivi de progression en polling
- Telechargement unitaire + ZIP

### Sprint 2 - Robustesse

- Historique persistant des jobs via `jobs.db` SQLite (implémente)
- Retry et timeouts OCR configurables via variables d'environnement (implémente)
- Logs structures avec evenements `job_id` / fichier (implémente)
- Rate limiting sur `/upload` (implémente)

### Sprint 3 - Industrialisation

- CI/CD GitHub Actions (`.github/workflows/ci.yml`) (implémente)
- Supervision via endpoint `/metrics` + script `scripts/monitor_health.sh` (implémente)
- HTTPS certbot + durcissement Nginx (`deploy/nginx_ocr_app_hardened.conf`) (implémente)
- Worker OCR separe du process web (`worker.py` + `deploy/ocr_worker.service`) (implémente)

## GitHub (publier le code)

Sur votre machine (dans le dossier `ocr_app/`), initialisez le depot puis poussez:

```bash
cd ocr_app
git init
git add -A
git -c user.name="LocaGed" -c user.email="you@example.com" commit -m "Initial import"
git branch -M main
git remote add origin git@github.com:<ORG_OU_USER>/<NOM_DU_REPO>.git
git push -u origin main
```

Ensuite sur le serveur:

```bash
sudo git clone git@github.com:<ORG_OU_USER>/<NOM_DU_REPO>.git /opt/ocr_app
```

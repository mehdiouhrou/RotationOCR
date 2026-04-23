import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("ocr_app")

USERS_FILE = BASE_DIR / "users.json"
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "jobs.db"
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
DEFAULT_PROCESSED_DIR = BASE_DIR / "fichiers_traites"


def resolve_processed_dir() -> Path:
    raw = (os.environ.get("PROCESSED_DIR") or "").strip()
    if not raw:
        return DEFAULT_PROCESSED_DIR

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (BASE_DIR / candidate).resolve()

    parts = candidate.parts
    if len(parts) >= 2 and parts[1] == "Users":
        logger.warning(
            "PROCESSED_DIR points to a macOS path (%s) on a Linux server; falling back to %s",
            str(candidate),
            str(DEFAULT_PROCESSED_DIR),
        )
        return DEFAULT_PROCESSED_DIR

    try:
        candidate.relative_to(BASE_DIR.resolve())
    except ValueError:
        logger.warning(
            "PROCESSED_DIR (%s) is outside project base (%s); falling back to %s",
            str(candidate),
            str(BASE_DIR.resolve()),
            str(DEFAULT_PROCESSED_DIR),
        )
        return DEFAULT_PROCESSED_DIR

    return candidate


PROCESSED_DIR = resolve_processed_dir()

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_sqlite_if_needed() -> None:
    """Older installs used BASE_DIR/jobs.db; WAL sidecars need a writable directory."""
    legacy_main = BASE_DIR / "jobs.db"
    if DB_FILE.exists() or not legacy_main.exists():
        return
    shutil.move(str(legacy_main), str(DB_FILE))
    for name in ("jobs.db-wal", "jobs.db-shm"):
        p = BASE_DIR / name
        if p.exists():
            shutil.move(str(p), str(DATA_DIR / name))


_migrate_legacy_sqlite_if_needed()

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(8 * 60 * 60)))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", str(30 * 60)))
TESSERACT_LANGS = os.environ.get("TESSERACT_LANGS", "fra+ara+eng")
MAX_FILES_PER_JOB = int(os.environ.get("MAX_FILES_PER_JOB", "500"))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(1024 * 1024 * 1024)))
OCR_TIMEOUT_SECONDS = int(os.environ.get("OCR_TIMEOUT_SECONDS", "1200"))
OCR_MAX_RETRIES = int(os.environ.get("OCR_MAX_RETRIES", "1"))
UPLOAD_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("UPLOAD_RATE_LIMIT_WINDOW_SECONDS", "60"))
UPLOAD_RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("UPLOAD_RATE_LIMIT_MAX_REQUESTS", "10"))
WORKER_POLL_SECONDS = int(os.environ.get("WORKER_POLL_SECONDS", "2"))


def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at REAL NOT NULL,
                total INTEGER NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                error_msg TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_files_job_id ON job_files(job_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")


def db_create_job(job_id, username, created_at, file_entries):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, username, created_at, total, done, status) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, username, created_at, len(file_entries), 0, "queued"),
        )
        conn.executemany(
            """
            INSERT INTO job_files(job_id, relative_path, original_name, status, progress, error_msg)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    job_id,
                    file_data["relative_path"],
                    file_data["original_name"],
                    file_data["status"],
                    int(file_data["progress"]),
                    file_data["error_msg"],
                )
                for file_data in file_entries
            ],
        )


def db_get_job(job_id):
    with get_db_connection() as conn:
        job_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not job_row:
            return None
        file_rows = conn.execute(
            """
            SELECT relative_path, original_name, status, progress, error_msg
            FROM job_files
            WHERE job_id = ?
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()

    files = [
        {
            "relative_path": row["relative_path"],
            "original_name": row["original_name"],
            "status": row["status"],
            "progress": int(row["progress"]),
            "error_msg": row["error_msg"] or "",
        }
        for row in file_rows
    ]

    return {
        "job_id": job_row["job_id"],
        "username": job_row["username"],
        "created_at": float(job_row["created_at"]),
        "total": int(job_row["total"]),
        "done": int(job_row["done"]),
        "status": job_row["status"],
        "files": files,
    }


def db_update_file(job_id, relative_path, status=None, progress=None, error_msg=None):
    updates = []
    values = []
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if progress is not None:
        updates.append("progress = ?")
        values.append(int(progress))
    if error_msg is not None:
        updates.append("error_msg = ?")
        values.append(error_msg)
    if not updates:
        return

    values.extend([job_id, relative_path])
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE job_files SET {', '.join(updates)} WHERE job_id = ? AND relative_path = ?",
            values,
        )


def db_increment_done(job_id):
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET done = done + 1,
                status = CASE
                    WHEN done + 1 >= total AND status != 'error' THEN 'finished'
                    ELSE status
                END
            WHERE job_id = ?
            """,
            (job_id,),
        )


def db_set_job_status(job_id, status):
    with get_db_connection() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))


def db_delete_job(job_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def db_list_expired_job_ids(expiration_before):
    with get_db_connection() as conn:
        rows = conn.execute("SELECT job_id FROM jobs WHERE created_at < ?", (expiration_before,)).fetchall()
    return [row["job_id"] for row in rows]


def db_claim_next_job():
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None

        job_id = row["job_id"]
        result = conn.execute(
            "UPDATE jobs SET status = 'processing' WHERE job_id = ? AND status = 'queued'",
            (job_id,),
        )
        if result.rowcount == 1:
            return job_id
    return None


def db_get_job_counts():
    with get_db_connection() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
    counts = {"queued": 0, "processing": 0, "finished": 0, "error": 0}
    for row in rows:
        counts[row["status"]] = int(row["count"])
    counts["total"] = sum(counts.values())
    return counts


def sanitize_relative_path(relative_path):
    raw = (relative_path or "").replace("\\", "/").strip()
    normalized = os.path.normpath(raw)

    if normalized in ("", "."):
        raise ValueError("Chemin vide")
    if os.path.isabs(normalized):
        raise ValueError("Chemin absolu interdit")
    if normalized.startswith("..") or "/../" in normalized:
        raise ValueError("Chemin invalide")

    return normalized


def validate_pdf_file(relative_path):
    return relative_path.lower().endswith(".pdf")


def dependency_status():
    """
    Vérifie la disponibilité des binaires externes requis par le système.
    
    Returns:
        dict: Statut de chaque dépendance avec clés:
            - pdftoppm: requis par pdf2image pour convertir PDF en images
            - tesseract: requis par pytesseract pour détection d'orientation
            - gs: requis pour la compression PDF avec Ghostscript
            - sqlite: toujours True (bibliothèque Python)
            - all_ok: True si toutes les dépendances binaires sont disponibles
    """
    required_bins = ["pdftoppm", "tesseract", "gs"]
    result = {}
    for binary in required_bins:
        result[binary] = bool(shutil.which(binary))
    result["sqlite"] = True
    result["all_ok"] = all(result[b] for b in required_bins)
    return result


def run_command(command, timeout_seconds=None, retries=None):
    timeout_seconds = timeout_seconds or OCR_TIMEOUT_SECONDS
    retries = OCR_MAX_RETRIES if retries is None else retries

    for attempt in range(1, retries + 2):
        try:
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=timeout_seconds,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as err:
            if attempt > retries:
                raise
            logger.warning(
                "ocr.retry command=%s attempt=%s timeout_seconds=%s error=%s",
                command[0],
                attempt,
                timeout_seconds,
                str(err)[:250],
            )
            time.sleep(min(attempt, 3))

    raise RuntimeError("run_command unexpected failure")


def list_sorted_files(directory, suffix):
    return sorted(
        [
            str(p)
            for p in Path(directory).iterdir()
            if p.is_file() and p.name.lower().endswith(suffix.lower())
        ]
    )


def build_export_folder_name(job_id: str, created_at: float) -> str:
    dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc).astimezone()
    stamp = dt.strftime("%Y%m%d_%H%M%S")
    short_id = str(job_id).split("-", 1)[0]
    return f"{stamp}_{short_id}"


def export_processed_outputs(job_id: str, created_at: float) -> dict:
    job = db_get_job(job_id)
    if not job:
        raise RuntimeError("Job introuvable")

    folder_name = build_export_folder_name(job_id, created_at)
    dest_root = PROCESSED_DIR / folder_name
    dest_root.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "username": job.get("username"),
        "created_at": float(created_at),
        "export_folder": folder_name,
        "export_path": str(dest_root.resolve()),
        "app": "locaged_ocr",
    }
    (dest_root / "EXPORT_META.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    copied = 0
    errors = []
    for file_entry in job["files"]:
        relative_path = file_entry["relative_path"]
        status = file_entry["status"]
        if status != "done":
            if status == "error" and file_entry.get("error_msg"):
                errors.append(f"{relative_path}: {file_entry['error_msg']}")
            continue

        src = OUTPUTS_DIR / job_id / relative_path
        if not src.exists():
            errors.append(f"{relative_path}: fichier de sortie manquant sur disque")
            continue

        dst = dest_root / relative_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    if errors:
        report_path = dest_root / "RAPPORT_ERREURS.txt"
        report_path.write_text("\n".join(errors) + "\n", encoding="utf-8")

    logger.info(
        "export.processed job_id=%s dest=%s copied=%s errors=%s",
        job_id,
        str(dest_root),
        copied,
        len(errors),
    )

    return {
        "export_folder": folder_name,
        "export_path": str(dest_root.resolve()),
        "export_copied": copied,
        "export_error_lines": len(errors),
    }


def export_processed_outputs_as_zip(job_id: str, created_at: float) -> dict:
    """
    Exporte les fichiers traités d'un job dans un fichier ZIP avec conservation de l'arborescence.
    
    Args:
        job_id: Identifiant du job
        created_at: Timestamp de création du job
        
    Returns:
        dict: Informations sur l'export ZIP avec clés:
            - export_folder: Nom du dossier d'export
            - export_path: Chemin absolu du dossier d'export
            - zip_path: Chemin absolu du fichier ZIP créé
            - zip_size: Taille du fichier ZIP en octets
            - export_copied: Nombre de fichiers copiés
            - export_error_lines: Nombre d'erreurs rencontrées
    """
    job = db_get_job(job_id)
    if not job:
        raise RuntimeError("Job introuvable")

    folder_name = build_export_folder_name(job_id, created_at)
    dest_root = PROCESSED_DIR / folder_name
    dest_root.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "username": job.get("username"),
        "created_at": float(created_at),
        "export_folder": folder_name,
        "export_path": str(dest_root.resolve()),
        "app": "locaged_ocr",
        "export_type": "zip",
    }
    (dest_root / "EXPORT_META.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    copied = 0
    errors = []
    
    # Créer un fichier ZIP temporaire
    import zipfile
    zip_filename = f"{folder_name}.zip"
    zip_path = PROCESSED_DIR / zip_filename
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Ajouter les fichiers traités au ZIP
        for file_entry in job["files"]:
            relative_path = file_entry["relative_path"]
            status = file_entry["status"]
            if status != "done":
                if status == "error" and file_entry.get("error_msg"):
                    errors.append(f"{relative_path}: {file_entry['error_msg']}")
                continue

            src = OUTPUTS_DIR / job_id / relative_path
            if not src.exists():
                errors.append(f"{relative_path}: fichier de sortie manquant sur disque")
                continue

            # Ajouter le fichier au ZIP avec son chemin relatif
            zipf.write(src, arcname=relative_path)
            copied += 1
        
        # Ajouter le fichier de métadonnées au ZIP
        meta_file_path = dest_root / "EXPORT_META.json"
        if meta_file_path.exists():
            zipf.write(meta_file_path, arcname="EXPORT_META.json")
        
        # Ajouter un rapport d'erreurs si nécessaire
        if errors:
            report_path = dest_root / "RAPPORT_ERREURS.txt"
            report_path.write_text("\n".join(errors) + "\n", encoding="utf-8")
            zipf.write(report_path, arcname="RAPPORT_ERREURS.txt")

    # Nettoyer le dossier temporaire (optionnel)
    import shutil
    shutil.rmtree(dest_root, ignore_errors=True)
    
    zip_size = zip_path.stat().st_size if zip_path.exists() else 0
    
    logger.info(
        "export.zip job_id=%s zip_path=%s zip_size=%s copied=%s errors=%s",
        job_id,
        str(zip_path),
        zip_size,
        copied,
        len(errors),
    )

    return {
        "export_folder": folder_name,
        "export_path": str(dest_root.resolve()),
        "zip_path": str(zip_path.resolve()),
        "zip_size": zip_size,
        "export_copied": copied,
        "export_error_lines": len(errors),
    }


def delete_exported_job(job_id: str) -> dict:
    """
    Supprime manuellement les fichiers exportés d'un job (dossier et ZIP).
    
    Args:
        job_id: Identifiant du job à supprimer
        
    Returns:
        dict: Résultat de la suppression avec clés:
            - success: True si la suppression a réussi
            - deleted_folders: Liste des dossiers supprimés
            - deleted_zips: Liste des fichiers ZIP supprimés
            - error: Message d'erreur en cas d'échec
    """
    try:
        deleted_folders = []
        deleted_zips = []
        
        # Chercher tous les dossiers d'export correspondant à ce job_id
        if PROCESSED_DIR.exists():
            for item in PROCESSED_DIR.iterdir():
                if item.is_dir() and job_id in item.name:
                    try:
                        shutil.rmtree(item)
                        deleted_folders.append(str(item))
                        logger.info(f"Suppression dossier export: {item}")
                    except Exception as e:
                        logger.warning(f"Erreur suppression dossier {item}: {e}")
                
                # Chercher aussi les fichiers ZIP correspondants
                if item.is_file() and item.suffix == '.zip' and job_id in item.name:
                    try:
                        item.unlink()
                        deleted_zips.append(str(item))
                        logger.info(f"Suppression ZIP export: {item}")
                    except Exception as e:
                        logger.warning(f"Erreur suppression ZIP {item}: {e}")
        
        # Supprimer aussi les fichiers dans uploads/ et outputs/
        uploads_job_dir = UPLOADS_DIR / job_id
        outputs_job_dir = OUTPUTS_DIR / job_id
        
        for job_dir in [uploads_job_dir, outputs_job_dir]:
            if job_dir.exists():
                try:
                    shutil.rmtree(job_dir)
                    logger.info(f"Suppression répertoire job: {job_dir}")
                except Exception as e:
                    logger.warning(f"Erreur suppression répertoire {job_dir}: {e}")
        
        # Supprimer l'entrée de la base de données
        db_delete_job(job_id)
        logger.info(f"Suppression entrée base de données pour job: {job_id}")
        
        return {
            "success": True,
            "deleted_folders": deleted_folders,
            "deleted_zips": deleted_zips,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"Erreur lors de la suppression du job {job_id}: {e}")
        return {
            "success": False,
            "deleted_folders": [],
            "deleted_zips": [],
            "error": str(e)
        }


def cleanup_expired_jobs() -> dict:
    """
    Nettoie automatiquement les jobs expirés selon le délai configuré (JOB_TTL_SECONDS).
    
    Returns:
        dict: Résultat du nettoyage avec clés:
            - cleaned_jobs: Liste des job_ids nettoyés
            - cleaned_folders: Nombre de dossiers supprimés
            - cleaned_zips: Nombre de fichiers ZIP supprimés
            - errors: Liste des erreurs rencontrées
    """
    try:
        now = time.time()
        expiration_before = now - JOB_TTL_SECONDS
        
        # Récupérer les jobs expirés
        expired_job_ids = db_list_expired_job_ids(expiration_before)
        
        cleaned_jobs = []
        cleaned_folders = 0
        cleaned_zips = 0
        errors = []
        
        for job_id in expired_job_ids:
            try:
                # Supprimer les fichiers exportés
                result = delete_exported_job(job_id)
                if result["success"]:
                    cleaned_jobs.append(job_id)
                    cleaned_folders += len(result["deleted_folders"])
                    cleaned_zips += len(result["deleted_zips"])
                else:
                    errors.append(f"Job {job_id}: {result['error']}")
            except Exception as e:
                errors.append(f"Job {job_id}: {str(e)}")
        
        logger.info(
            "cleanup.expired cleaned_jobs=%s cleaned_folders=%s cleaned_zips=%s errors=%s",
            len(cleaned_jobs),
            cleaned_folders,
            cleaned_zips,
            len(errors),
        )
        
        return {
            "cleaned_jobs": cleaned_jobs,
            "cleaned_folders": cleaned_folders,
            "cleaned_zips": cleaned_zips,
            "errors": errors
        }
        
    except Exception as e:
        logger.error(f"Erreur lors du nettoyage automatique: {e}")
        return {
            "cleaned_jobs": [],
            "cleaned_folders": 0,
            "cleaned_zips": 0,
            "errors": [str(e)]
        }


def schedule_automatic_cleanup():
    """
    Planifie le nettoyage automatique périodique des jobs expirés.
    À exécuter dans un thread séparé ou via un scheduler externe.
    """
    import threading
    import time as time_module
    
    def cleanup_loop():
        while True:
            try:
                result = cleanup_expired_jobs()
                if result["cleaned_jobs"]:
                    logger.info(f"Nettoyage automatique effectué: {len(result['cleaned_jobs'])} jobs nettoyés")
            except Exception as e:
                logger.error(f"Erreur dans la boucle de nettoyage: {e}")
            
            # Attendre l'intervalle configuré
            time_module.sleep(CLEANUP_INTERVAL_SECONDS)
    
    # Démarrer le thread de nettoyage
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    logger.info(f"Nettoyage automatique démarré (intervalle: {CLEANUP_INTERVAL_SECONDS}s)")
    return cleanup_thread

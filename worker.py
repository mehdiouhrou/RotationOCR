import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from job_store import (
    MAX_WORKERS,
    OUTPUTS_DIR,
    UPLOADS_DIR,
    WORKER_POLL_SECONDS,
    db_claim_next_job,
    db_get_job,
    db_increment_done,
    db_set_job_status,
    db_update_file,
    export_processed_outputs,
    init_db,
)

from rotation_service import process_pdf_rotation

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rotation_worker")


def process_single_pdf(job_id, file_entry):
    relative_path = file_entry["relative_path"]
    input_pdf_path = UPLOADS_DIR / job_id / relative_path
    output_pdf_path = OUTPUTS_DIR / job_id / relative_path
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    db_update_file(job_id, relative_path, status="processing", progress=10, error_msg="")
    logger.info("rotation.file.start job_id=%s file=%s", job_id, relative_path)

    try:
        # Traitement de rotation avec notre nouveau service
        output_path, rotation_angle, success = process_pdf_rotation(
            str(input_pdf_path),
            str(output_pdf_path.parent)
        )
        
        if success and output_path:
            # Mettre à jour la progression
            db_update_file(job_id, relative_path, progress=100)
            
            # Mettre à jour le statut avec l'angle de rotation détecté
            status_msg = f"Rotation corrigée ({rotation_angle}°)" if rotation_angle != 0 else "Aucune rotation nécessaire"
            db_update_file(
                job_id, 
                relative_path, 
                status="done", 
                progress=100, 
                error_msg=status_msg
            )
            logger.info("rotation.file.done job_id=%s file=%s angle=%s", 
                       job_id, relative_path, rotation_angle)
        else:
            # En cas d'échec, copier le fichier original comme fallback
            shutil.copy2(str(input_pdf_path), str(output_pdf_path))
            db_update_file(
                job_id, 
                relative_path, 
                status="done", 
                progress=100, 
                error_msg="Rotation échouée, fichier original copié"
            )
            logger.warning("rotation.file.fallback job_id=%s file=%s", job_id, relative_path)
            
    except Exception as err:  # noqa: BLE001
        err_msg = str(err)[:500]
        db_update_file(job_id, relative_path, status="error", progress=100, error_msg=err_msg)
        logger.error("rotation.file.error job_id=%s file=%s error=%s", job_id, relative_path, err_msg)
    finally:
        db_increment_done(job_id)


def process_job(job_id):
    job = db_get_job(job_id)
    if not job:
        return

    logger.info("rotation.job.start job_id=%s total=%s", job_id, job["total"])
    files = job["files"]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_single_pdf, job_id, file_entry) for file_entry in files]
        for future in as_completed(futures):
            future.result()

    final_job = db_get_job(job_id) or job
    has_error = any(file_entry.get("status") == "error" for file_entry in final_job.get("files", []))
    db_set_job_status(job_id, "error" if has_error else "finished")

    export_info = export_processed_outputs(job_id, float(final_job["created_at"]))
    logger.info(
        "rotation.job.export job_id=%s folder=%s copied=%s",
        job_id,
        export_info.get("export_folder"),
        export_info.get("export_copied"),
    )
    logger.info("rotation.job.finished job_id=%s status=%s", job_id, "error" if has_error else "finished")


def main():
    init_db()
    logger.info("worker.start poll_seconds=%s", WORKER_POLL_SECONDS)
    while True:
        job_id = db_claim_next_job()
        if not job_id:
            time.sleep(WORKER_POLL_SECONDS)
            continue

        try:
            process_job(job_id)
        except Exception as err:  # noqa: BLE001
            db_set_job_status(job_id, "error")
            logger.exception("worker.job.crash job_id=%s error=%s", job_id, str(err)[:250])


if __name__ == "__main__":
    main()

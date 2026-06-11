import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF

import ai_titler
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
    db_update_file_ai,
    export_processed_outputs,
    init_db,
)
from rotation_service import process_pdf_rotation

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rotation_worker")


def _extract_ocr_text(pdf_path: str) -> str:
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as exc:
        logger.warning("ocr_text.extract_failed path=%s error=%s", pdf_path, exc)
        return ""


def process_single_pdf(job_id, file_entry):
    relative_path = file_entry["relative_path"]
    original_filename = file_entry["original_name"]
    input_pdf_path = UPLOADS_DIR / job_id / relative_path
    output_pdf_path = OUTPUTS_DIR / job_id / relative_path
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    db_update_file(job_id, relative_path, status="processing", progress=10, error_msg="")
    logger.info("rotation.file.start job_id=%s file=%s", job_id, relative_path)

    try:
        output_path, rotation_angle, success = process_pdf_rotation(
            str(input_pdf_path),
            str(output_pdf_path.parent),
        )

        if success and output_path:
            db_update_file(job_id, relative_path, progress=90)

            # Extraction du texte OCR puis renommage IA
            ocr_text = _extract_ocr_text(output_path)
            ai_result = ai_titler.extract_title(ocr_text, original_filename)

            new_path = Path(output_path).parent / ai_result["nom_fichier"]
            try:
                os.rename(output_path, new_path)
                logger.info(
                    "ai_titler.renamed job_id=%s file=%s new_name=%s confiance=%s statut=%s",
                    job_id,
                    relative_path,
                    ai_result["nom_fichier"],
                    ai_result["confiance"],
                    ai_result["statut"],
                )
            except Exception as rename_err:
                logger.warning(
                    "ai_titler.rename_failed job_id=%s file=%s error=%s",
                    job_id,
                    relative_path,
                    rename_err,
                )
                ai_result["nom_fichier"] = Path(output_path).name

            db_update_file_ai(
                job_id,
                relative_path,
                titre_final=ai_result["titre"],
                statut_titre=ai_result["statut"],
                confiance=ai_result["confiance"],
                nom_fichier=ai_result["nom_fichier"],
            )

            status_msg = (
                f"Rotation corrigée ({rotation_angle}°)"
                if rotation_angle != 0
                else "Aucune rotation nécessaire"
            )
            db_update_file(
                job_id,
                relative_path,
                status="done",
                progress=100,
                error_msg=status_msg,
            )
            logger.info(
                "rotation.file.done job_id=%s file=%s angle=%s",
                job_id,
                relative_path,
                rotation_angle,
            )
        else:
            shutil.copy2(str(input_pdf_path), str(output_pdf_path))
            db_update_file(
                job_id,
                relative_path,
                status="done",
                progress=100,
                error_msg="Rotation échouée, fichier original copié",
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

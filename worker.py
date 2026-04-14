import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from job_store import (
    MAX_WORKERS,
    OCR_TIMEOUT_SECONDS,
    OUTPUTS_DIR,
    TESSERACT_LANGS,
    UPLOADS_DIR,
    WORKER_POLL_SECONDS,
    db_claim_next_job,
    db_get_job,
    db_increment_done,
    db_set_job_status,
    db_update_file,
    export_processed_outputs,
    init_db,
    list_sorted_files,
    run_command,
)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ocr_worker")


def process_single_pdf(job_id, file_entry):
    relative_path = file_entry["relative_path"]
    input_pdf_path = UPLOADS_DIR / job_id / relative_path
    output_pdf_path = OUTPUTS_DIR / job_id / relative_path
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    temp_base_dir = UPLOADS_DIR / job_id / "_tmp" / relative_path.replace("/", "_")
    temp_base_dir.mkdir(parents=True, exist_ok=True)
    pages_prefix = temp_base_dir / "page"

    db_update_file(job_id, relative_path, status="processing", progress=5, error_msg="")
    logger.info("ocr.file.start job_id=%s file=%s", job_id, relative_path)

    try:
        run_command(
            ["pdftoppm", "-r", "300", str(input_pdf_path), str(pages_prefix)],
            timeout_seconds=OCR_TIMEOUT_SECONDS,
        )
        db_update_file(job_id, relative_path, progress=20)

        ppm_files = list_sorted_files(temp_base_dir, ".ppm")
        if not ppm_files:
            raise RuntimeError("Aucune image PPM generee par pdftoppm")

        page_pdfs = []
        total_pages = len(ppm_files)
        for page_i, ppm_path in enumerate(ppm_files, start=1):
            output_prefix = str(Path(ppm_path).with_suffix(""))
            run_command(
                [
                    "tesseract",
                    ppm_path,
                    output_prefix,
                    "-l",
                    TESSERACT_LANGS,
                    "--psm",
                    "1",
                    "pdf",
                ],
                timeout_seconds=OCR_TIMEOUT_SECONDS,
            )
            page_pdf = f"{output_prefix}.pdf"
            if not Path(page_pdf).exists():
                raise RuntimeError(f"Page OCR manquante: {page_pdf}")
            page_pdfs.append(page_pdf)
            page_progress = 20 + int((page_i / total_pages) * 60)
            db_update_file(job_id, relative_path, progress=page_progress)

        run_command(
            [
                "gs",
                "-dBATCH",
                "-dNOPAUSE",
                "-sDEVICE=pdfwrite",
                "-dPDFSETTINGS=/ebook",
                f"-sOutputFile={output_pdf_path}",
                *page_pdfs,
            ],
            timeout_seconds=OCR_TIMEOUT_SECONDS,
        )

        db_update_file(job_id, relative_path, status="done", progress=100, error_msg="")
        logger.info("ocr.file.done job_id=%s file=%s", job_id, relative_path)
    except Exception as err:  # noqa: BLE001
        err_msg = str(err)[:500]
        db_update_file(job_id, relative_path, status="error", progress=100, error_msg=err_msg)
        logger.error("ocr.file.error job_id=%s file=%s error=%s", job_id, relative_path, err_msg)
    finally:
        db_increment_done(job_id)
        shutil.rmtree(temp_base_dir, ignore_errors=True)


def process_job(job_id):
    job = db_get_job(job_id)
    if not job:
        return

    logger.info("ocr.job.start job_id=%s total=%s", job_id, job["total"])
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
        "ocr.job.export job_id=%s folder=%s copied=%s",
        job_id,
        export_info.get("export_folder"),
        export_info.get("export_copied"),
    )
    logger.info("ocr.job.finished job_id=%s status=%s", job_id, "error" if has_error else "finished")


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

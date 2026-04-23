import atexit
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Optional

import bcrypt
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

from job_store import (
    CLEANUP_INTERVAL_SECONDS,
    JOB_TTL_SECONDS,
    MAX_CONTENT_LENGTH,
    MAX_FILES_PER_JOB,
    OUTPUTS_DIR,
    PROCESSED_DIR,
    UPLOADS_DIR,
    UPLOAD_RATE_LIMIT_MAX_REQUESTS,
    UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
    USERS_FILE,
    build_export_folder_name,
    db_create_job,
    db_delete_job,
    db_get_job,
    db_get_job_counts,
    db_list_expired_job_ids,
    dependency_status,
    init_db,
    sanitize_relative_path,
    validate_pdf_file,
)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rotation_web")

SAFE_EXPORT_FOLDER_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-fA-F-]{8,}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

cleanup_stop_event = threading.Event()
upload_rate_map = {}
rate_limit_lock = threading.Lock()


def log_event(event_name, **fields):
    details = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    logger.info("%s %s", event_name, details)


def get_job_owner(job_id):
    db_job = db_get_job(job_id)
    if db_job:
        return db_job.get("username")
    return None


def check_upload_rate_limit(username):
    now = time.time()
    with rate_limit_lock:
        history = upload_rate_map.get(username, [])
        history = [ts for ts in history if now - ts <= UPLOAD_RATE_LIMIT_WINDOW_SECONDS]
        if len(history) >= UPLOAD_RATE_LIMIT_MAX_REQUESTS:
            retry_after = int(UPLOAD_RATE_LIMIT_WINDOW_SECONDS - (now - history[0])) + 1
            upload_rate_map[username] = history
            return False, max(retry_after, 1)

        history.append(now)
        upload_rate_map[username] = history
    return True, 0


def load_users():
    if not USERS_FILE.exists():
        return {}

    with USERS_FILE.open("r", encoding="utf-8") as f:
        raw_users = json.load(f)

    users_by_name = {}
    for item in raw_users:
        username = str(item.get("username", "")).strip()
        password_hash = str(item.get("password", "")).strip()
        if username and password_hash:
            users_by_name[username] = password_hash
    return users_by_name


def verify_password(username, password):
    users = load_users()
    hash_value = users.get(username)
    if not hash_value:
        return False

    try:
        return bcrypt.checkpw(password.encode("utf-8"), hash_value.encode("utf-8"))
    except ValueError:
        return False


def login_required(view_fn):
    @wraps(view_fn)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return view_fn(*args, **kwargs)

    return wrapped


def is_safe_export_folder_name(folder_name: str) -> bool:
    return bool(folder_name) and SAFE_EXPORT_FOLDER_RE.match(folder_name) is not None


def read_export_meta(export_dir: Path) -> Optional[dict]:
    meta_path = export_dir / "EXPORT_META.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_user_exports(username: str):
    exports = []
    if not PROCESSED_DIR.exists():
        return exports

    for child in sorted(PROCESSED_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not child.is_dir():
            continue
        if not is_safe_export_folder_name(child.name):
            continue
        meta = read_export_meta(child)
        if not meta:
            continue
        if meta.get("username") != username:
            continue

        pdf_count = sum(1 for p in child.rglob("*.pdf"))
        exports.append(
            {
                "folder": child.name,
                "path": str(child.resolve()),
                "created_at": float(meta.get("created_at", 0)),
                "job_id": meta.get("job_id"),
                "pdf_count": pdf_count,
                "has_error_report": (child / "RAPPORT_ERREURS.txt").exists(),
            }
        )

    return exports


def safe_join_under(root: Path, rel_path: str) -> Path:
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    target = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if root_resolved == target or root_resolved in target.parents:
        return target
    abort(404)


def create_zip_for_job(job_id):
    job = db_get_job(job_id)
    if not job:
        return None

    export_name = build_export_folder_name(job_id, float(job["created_at"]))
    export_dir = PROCESSED_DIR / export_name

    if export_dir.exists():
        zip_base = PROCESSED_DIR / f"{export_name}_export"
        zip_file = Path(f"{zip_base}.zip")
        if zip_file.exists():
            zip_file.unlink()
        shutil.make_archive(str(zip_base), "zip", root_dir=export_dir)
        return zip_file

    job_output_dir = OUTPUTS_DIR / job_id
    if not job_output_dir.exists():
        return None

    zip_base = OUTPUTS_DIR / f"{job_id}_all"
    zip_file = Path(f"{zip_base}.zip")
    if zip_file.exists():
        zip_file.unlink()

    shutil.make_archive(str(zip_base), "zip", root_dir=job_output_dir)
    return zip_file


def cleanup_expired_jobs():
    while not cleanup_stop_event.is_set():
        now = time.time()
        expiration_before = now - JOB_TTL_SECONDS
        expired_ids = db_list_expired_job_ids(expiration_before)

        for job_id in expired_ids:
            shutil.rmtree(UPLOADS_DIR / job_id, ignore_errors=True)
            shutil.rmtree(OUTPUTS_DIR / job_id, ignore_errors=True)
            zip_file = OUTPUTS_DIR / f"{job_id}_all.zip"
            if zip_file.exists():
                zip_file.unlink(missing_ok=True)

            job_snapshot = db_get_job(job_id)
            if job_snapshot:
                export_name = build_export_folder_name(job_id, float(job_snapshot["created_at"]))
                export_zip = PROCESSED_DIR / f"{export_name}_export.zip"
                if export_zip.exists():
                    export_zip.unlink(missing_ok=True)

            db_delete_job(job_id)
            log_event("cleanup.job_deleted", job_id=job_id)

        cleanup_stop_event.wait(CLEANUP_INTERVAL_SECONDS)


init_db()
cleanup_thread = threading.Thread(target=cleanup_expired_jobs, daemon=True)
cleanup_thread.start()


@atexit.register
def _stop_cleanup_thread():
    cleanup_stop_event.set()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("username"):
            return redirect(url_for("index"))
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if verify_password(username, password):
        session.clear()
        session["username"] = username
        session.permanent = False
        return redirect(url_for("index"))

    return render_template("login.html", error_message="Identifiants invalides.")


@app.route("/logout", methods=["GET"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("index.html", username=session.get("username"))


@app.route("/exports", methods=["GET"])
@login_required
def exports_page():
    return render_template("exports.html", username=session.get("username"))


@app.route("/exports/api", methods=["GET"])
@login_required
def exports_api():
    return jsonify({"exports": list_user_exports(session.get("username"))})


@app.route("/exports/browse/<export_folder>", methods=["GET"])
@login_required
def exports_browse(export_folder):
    if not is_safe_export_folder_name(export_folder):
        abort(404)

    export_dir = (PROCESSED_DIR / export_folder).resolve()
    if not export_dir.exists() or not export_dir.is_dir():
        abort(404)

    meta = read_export_meta(export_dir)
    if not meta or meta.get("username") != session.get("username"):
        abort(403)

    rel_prefix = (request.args.get("path") or "").replace("\\", "/")
    browse_root = safe_join_under(export_dir, rel_prefix)
    if not browse_root.exists() or not browse_root.is_dir():
        abort(404)

    entries = []
    for child in sorted(browse_root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel = str(child.resolve().relative_to(export_dir.resolve())).replace("\\", "/")
        entries.append(
            {
                "name": child.name,
                "rel_path": rel,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            }
        )

    return jsonify(
        {
            "export_folder": export_folder,
            "meta": meta,
            "path": rel_prefix,
            "entries": entries,
            "has_error_report": (export_dir / "RAPPORT_ERREURS.txt").exists(),
        }
    )


@app.route("/exports/file/<export_folder>/<path:relative_path>", methods=["GET"])
@login_required
def exports_file(export_folder, relative_path):
    if not is_safe_export_folder_name(export_folder):
        abort(404)

    export_dir = (PROCESSED_DIR / export_folder).resolve()
    if not export_dir.exists() or not export_dir.is_dir():
        abort(404)

    meta = read_export_meta(export_dir)
    if not meta or meta.get("username") != session.get("username"):
        abort(403)

    file_path = safe_join_under(export_dir, relative_path)
    if not file_path.exists() or not file_path.is_file():
        abort(404)

    download = request.args.get("download") == "1"
    return send_file(
        file_path,
        as_attachment=download,
        download_name=file_path.name if download else None,
        conditional=True,
        max_age=0,
    )


@app.route("/health", methods=["GET"])
def health():
    deps = dependency_status()
    status_code = 200 if deps["all_ok"] else 500
    return jsonify({"ok": deps["all_ok"], "dependencies": deps}), status_code


@app.route("/metrics", methods=["GET"])
@login_required
def metrics():
    return jsonify({"jobs": db_get_job_counts()})


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = request.files.getlist("files")
    relative_paths = request.form.getlist("relative_paths")
    username = session.get("username")

    allowed, retry_after = check_upload_rate_limit(username)
    if not allowed:
        return (
            jsonify(
                {
                    "error": "Trop de requetes upload, reessayez plus tard",
                    "retry_after_seconds": retry_after,
                }
            ),
            429,
        )

    if not files:
        return jsonify({"error": "Aucun fichier recu"}), 400
    if len(files) != len(relative_paths):
        return jsonify({"error": "Liste des chemins relative invalide"}), 400
    if len(files) > MAX_FILES_PER_JOB:
        return jsonify({"error": f"Trop de fichiers: maximum {MAX_FILES_PER_JOB}"}), 400

    job_id = str(uuid.uuid4())
    job_upload_dir = UPLOADS_DIR / job_id
    job_output_dir = OUTPUTS_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    job_output_dir.mkdir(parents=True, exist_ok=True)

    sanitized_paths = []
    for rel_path_raw in relative_paths:
        try:
            rel_path = sanitize_relative_path(rel_path_raw)
        except ValueError as err:
            shutil.rmtree(job_upload_dir, ignore_errors=True)
            shutil.rmtree(job_output_dir, ignore_errors=True)
            return jsonify({"error": f"Chemin refuse: {err}"}), 400

        if not validate_pdf_file(rel_path):
            shutil.rmtree(job_upload_dir, ignore_errors=True)
            shutil.rmtree(job_output_dir, ignore_errors=True)
            return jsonify({"error": f"Fichier non-PDF: {rel_path}"}), 400

        sanitized_paths.append(rel_path)

    file_entries = []
    for file_obj, rel_path in zip(files, sanitized_paths):
        target_path = job_upload_dir / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_obj.save(target_path)

        file_entries.append(
            {
                "relative_path": rel_path,
                "original_name": Path(rel_path).name,
                "status": "pending",
                "progress": 0,
                "error_msg": "",
            }
        )

    created_at = time.time()
    db_create_job(job_id, username, created_at, file_entries)
    log_event("upload.job_created", job_id=job_id, username=username, total=len(file_entries))

    return jsonify({"job_id": job_id, "total": len(file_entries)})


@app.route("/status/<job_id>", methods=["GET"])
@login_required
def status(job_id):
    job = db_get_job(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    if job.get("username") != session.get("username"):
        return jsonify({"error": "Acces refuse"}), 403

    done = job["done"]
    total = job["total"]
    if total:
        progress_sum = sum(int(file_data.get("progress", 0)) for file_data in job["files"])
        global_progress = int(progress_sum / total)
    else:
        global_progress = 0

    export_payload = None
    if job.get("status") in {"finished", "error"}:
        export_name = build_export_folder_name(job_id, float(job["created_at"]))
        export_dir = PROCESSED_DIR / export_name
        if export_dir.exists():
            done_files = sum(1 for f in job["files"] if f.get("status") == "done")
            error_files = sum(1 for f in job["files"] if f.get("status") == "error")
            export_payload = {
                "folder_name": export_name,
                "path": str(export_dir.resolve()),
                "done_files": done_files,
                "error_files": error_files,
                "has_error_report": (export_dir / "RAPPORT_ERREURS.txt").exists(),
            }

    return jsonify(
        {
            "job_id": job_id,
            "created_at": job["created_at"],
            "job_status": job.get("status"),
            "total": total,
            "done": done,
            "global_progress": global_progress,
            "files": job["files"],
            "export": export_payload,
        }
    )


@app.route("/download/<job_id>/<path:relative_path>", methods=["GET"])
@login_required
def download_file(job_id, relative_path):
    owner = get_job_owner(job_id)
    if owner is None:
        abort(404)
    if owner != session.get("username"):
        abort(403)

    try:
        safe_rel_path = sanitize_relative_path(relative_path)
    except ValueError:
        abort(400)

    file_path = OUTPUTS_DIR / job_id / safe_rel_path
    if not file_path.exists():
        abort(404)

    return send_file(
        file_path,
        as_attachment=True,
        download_name=Path(safe_rel_path).name,
    )


@app.route("/download_all/<job_id>", methods=["GET"])
@login_required
def download_all(job_id):
    job = db_get_job(job_id)
    if not job:
        abort(404)
    if job.get("username") != session.get("username"):
        abort(403)
    if job.get("done") != job.get("total"):
        return jsonify({"error": "Job non termine"}), 409

    zip_file = create_zip_for_job(job_id)
    if zip_file is None or not zip_file.exists():
        abort(404)

    return send_file(zip_file, as_attachment=True, download_name=f"rotation_{job_id}.zip")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)

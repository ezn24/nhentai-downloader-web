from __future__ import annotations

from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, jsonify
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid

latest_log_output = ""


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue

            os.environ[key] = clean_env_value(value)


app = Flask(__name__, template_folder="./html")
app.secret_key = os.urandom(24)

PASSWORD = os.getenv("NHENTAI_PASSWORD", "admin")

MAX_TASK_HISTORY = 50
task_queue: "queue.Queue[dict | None]" = queue.Queue()
task_history = []
task_lock = threading.Lock()


def clean_env_value(value: str, default: str = "") -> str:
    value = (value or default).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


load_dotenv()


def get_download_path() -> str:
    raw_path = clean_env_value(os.getenv("DOWNLOAD_PATH", "/nhentai"), "/nhentai")
    if raw_path.startswith("/"):
        return raw_path
    return os.path.abspath(os.path.expanduser(raw_path))


def get_default_format() -> str:
    return clean_env_value(os.getenv("DEFAULT_FORMAT", "%a%t"), "%a%t")


def get_doujinshi_url() -> str:
    return clean_env_value(os.getenv("DOUJINSHI_DL_URL", "https://nhentai.net"), "https://nhentai.net")


def get_doujinshi_token() -> str:
    return clean_env_value(os.getenv("DOUJINSHI_DL_TOKEN", ""))


def doujinshi_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOUJINSHI_DL_URL"] = get_doujinshi_url()
    return env


def configure_env_token() -> str:
    token = get_doujinshi_token()
    if not token:
        return ""

    result = subprocess.run(
        ["doujinshi-dl", "--token", token],
        capture_output=True,
        text=True,
        env=doujinshi_env(),
    )
    return (result.stdout or "") + (result.stderr or "")


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _summarize_result(output: str, returncode: int | None) -> tuple[str, str]:
    if returncode == 0:
        return "success", "Download finished"
    return "failed", "Command failed"


STATUS_DISPLAY = {
    "queued": "Queued",
    "running": "Running",
    "success": "Success",
    "failed": "Failed",
}


def _serialize_task(task: dict) -> dict:
    preview = (task.get("output") or "").strip()
    if len(preview) > 400:
        preview = preview[:397] + "..."

    duration_value = task.get("duration")
    duration = f"{duration_value:.1f}s" if duration_value else None

    return {
        "id": task.get("id"),
        "label": task.get("label"),
        "status": task.get("status"),
        "status_display": STATUS_DISPLAY.get(task.get("status"), task.get("status", "")),
        "created_at": _format_timestamp(task.get("created_at")),
        "started_at": _format_timestamp(task.get("started_at")),
        "finished_at": _format_timestamp(task.get("finished_at")),
        "summary": task.get("summary"),
        "returncode": task.get("returncode"),
        "duration": duration,
        "preview": preview,
    }


def _enqueue_task(args: list[str], label: str | None = None, output_dir: str | None = None) -> dict:
    task = {
        "id": uuid.uuid4().hex[:8],
        "label": label or " ".join(args),
        "command": args,
        "status": "queued",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "returncode": None,
        "summary": None,
        "output": "",
        "output_dir": output_dir,
        "duration": None,
    }

    with task_lock:
        task_history.insert(0, task)
        if len(task_history) > MAX_TASK_HISTORY:
            task_history.pop()

    task_queue.put(task)
    return task


def _command_worker() -> None:
    global latest_log_output

    while True:
        task = task_queue.get()
        if task is None:
            break

        with task_lock:
            task["status"] = "running"
            task["started_at"] = time.time()

        try:
            token_output = configure_env_token()
            output_dir = task.get("output_dir")
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            result = subprocess.run(
                task["command"],
                capture_output=True,
                text=True,
                env=doujinshi_env(),
            )
            output = (
                f"Output directory: {output_dir}\n"
                f"Command: {' '.join(task['command'])}\n\n"
                + token_output
                + (result.stdout or "")
                + (result.stderr or "")
            )
            returncode = result.returncode
        except Exception as exc:
            output = str(exc)
            returncode = None

        latest_log_output = output
        finished_at = time.time()
        status, summary = _summarize_result(output, returncode)

        with task_lock:
            task["finished_at"] = finished_at
            if task["started_at"]:
                task["duration"] = max(finished_at - task["started_at"], 0.0)
            task["returncode"] = returncode
            task["output"] = output
            task["status"] = status
            task["summary"] = summary

        task_queue.task_done()


worker_thread = threading.Thread(target=_command_worker, daemon=True)
worker_thread.start()


def run_doujinshi_command(
        args: list[str],
        label: str | None = None,
        output_dir: str | None = None,
) -> dict:
    return _enqueue_task(args, label, output_dir)


def parse_gallery_ids(raw) -> tuple[list[str], str | None]:
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                pass

    if isinstance(raw, list):
        parts = [str(part).strip() for part in raw if str(part).strip()]
    else:
        parts = [part for part in re.split(r"[\s,]+", str(raw or "").strip()) if part]

    if not parts or any((not p.isdigit()) or len(p) != 6 for p in parts):
        return [], "Invalid ID format (Use six-digit numbers separated by spaces, commas, or new lines)"

    seen = set()
    id_list = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            id_list.append(p)

    if len(id_list) > 50:
        return [], "Too many IDs at once (Max 50)"

    return id_list, None


def enqueue_download(id_list: list[str]) -> dict:
    output_dir = get_download_path()
    name_format = get_default_format()

    command = [
        "doujinshi-dl",
        "--id",
        *id_list,
        "--page-all",
        "--download",
        "--delay",
        "1",
        "--meta",
        "--cbz",
        "--format",
        name_format,
        "--rm-origin-dir",
        "--output",
        output_dir,
    ]
    return run_doujinshi_command(command, label=f"Download {len(id_list)} ID(s)", output_dir=output_dir)


def api_password_verified() -> bool:
    password = request.headers.get("X-API-Password", "")
    if not password:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            password = auth_header[7:].strip()

    return password == PASSWORD


@app.route("/")
def index():
    password_cookie = request.cookies.get("nhentai_auth")
    is_verified = password_cookie == "ok"
    return render_template("index.html", password=PASSWORD, verified=is_verified)


@app.route("/debug-log")
def debug_log():
    return latest_log_output, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/queue-status")
def queue_status():
    with task_lock:
        serialized = [_serialize_task(task) for task in task_history]

    pending = sum(1 for item in serialized if item["status"] in {"queued", "running"})
    return jsonify({
        "tasks": serialized,
        "pending": pending,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


@app.route("/verify-password", methods=["POST"])
def verify_password():
    password = request.form.get("password", "")
    if password == PASSWORD:
        resp = make_response(redirect(url_for("index")))
        resp.set_cookie("nhentai_auth", "ok", max_age=60 * 60 * 24 * 30)
        return resp

    flash("Wrong password", "error")
    return redirect(url_for("index"))


@app.route("/download", methods=["POST"])
def download():
    raw = request.form.get("id", "").strip()
    id_list, error = parse_gallery_ids(raw)
    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    task = enqueue_download(id_list)
    flash(f"Queued download task #{task['id']} for {len(id_list)} ID(s)", "success")
    return redirect(url_for("index"))


@app.route("/api/download", methods=["POST"])
def api_download():
    if not api_password_verified():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_ids = (
        payload.get("ids")
        or payload.get("id")
        or payload.get("gallery_id")
        or request.form.get("ids")
        or request.form.get("id")
        or request.form.get("gallery_id")
        or request.args.get("ids")
        or request.args.get("id")
        or request.args.get("gallery_id")
    )

    id_list, error = parse_gallery_ids(raw_ids)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    task = enqueue_download(id_list)
    return jsonify({
        "ok": True,
        "task": _serialize_task(task),
        "ids": id_list,
        "output_dir": task.get("output_dir"),
        "status_url": url_for("queue_status", _external=False),
    }), 202


if __name__ == "__main__":
    print(f"DOWNLOAD_PATH={get_download_path()}")
    print(f"DOUJINSHI_DL_URL={get_doujinshi_url()}")
    print(f"DOUJINSHI_DL_TOKEN={'set' if get_doujinshi_token() else 'not set'}")
    app.run(debug=True, host="0.0.0.0", port=61234)

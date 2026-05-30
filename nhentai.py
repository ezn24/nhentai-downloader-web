from __future__ import annotations

from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, jsonify
import os
import queue
import re
import subprocess
import threading
import time
import uuid

latest_log_output = ""

app = Flask(__name__, template_folder="./html")
app.secret_key = os.urandom(24)

PASSWORD = os.getenv("NHENTAI_PASSWORD", "admin")

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/nhentai")
DEFAULT_FORMAT = os.getenv("DEFAULT_FORMAT", "%a%t")
DOUJINSHI_DL_URL = os.getenv("DOUJINSHI_DL_URL", "https://nhentai.net")
DOUJINSHI_DL_TOKEN = os.getenv("DOUJINSHI_DL_TOKEN", "").strip()

MAX_TASK_HISTORY = 50
task_queue: "queue.Queue[dict | None]" = queue.Queue()
task_history = []
task_lock = threading.Lock()


def doujinshi_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOUJINSHI_DL_URL"] = DOUJINSHI_DL_URL
    return env


def configure_env_token() -> str:
    if not DOUJINSHI_DL_TOKEN:
        return ""

    result = subprocess.run(
        ["doujinshi-dl", "--token", DOUJINSHI_DL_TOKEN],
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


def _enqueue_task(args: list[str], label: str | None = None) -> dict:
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
            result = subprocess.run(
                task["command"],
                capture_output=True,
                text=True,
                env=doujinshi_env(),
            )
            output = token_output + (result.stdout or "") + (result.stderr or "")
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


def run_doujinshi_command(args: list[str], label: str | None = None) -> dict:
    return _enqueue_task(args, label)


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
    parts = [part for part in re.split(r"[\s,]+", raw) if part]

    if not DOUJINSHI_DL_TOKEN:
        flash("DOUJINSHI_DL_TOKEN is required in the deployment environment", "error")
        return redirect(url_for("index"))

    if not parts or any((not p.isdigit()) or len(p) != 6 for p in parts):
        flash("Invalid ID format (Use six-digit numbers separated by spaces, commas, or new lines)", "error")
        return redirect(url_for("index"))

    seen = set()
    id_list = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            id_list.append(p)

    if len(id_list) > 50:
        flash("Too many IDs at once (Max 50)", "error")
        return redirect(url_for("index"))

    command = [
        "doujinshi-dl",
        "--id",
        *id_list,
        "--page-all",
        "--download",
        "--delay",
        "1",
        "--cbz",
        "--format",
        DEFAULT_FORMAT,
        "--rm-origin-dir",
        "--output",
        DOWNLOAD_PATH,
    ]
    task = run_doujinshi_command(command, label=f"Download {len(id_list)} ID(s)")
    flash(f"Queued download task #{task['id']} for {len(id_list)} ID(s)", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=61234)

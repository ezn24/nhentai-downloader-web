from __future__ import annotations

from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, jsonify
import queue
import subprocess
import threading
import time
import os
import uuid

latest_log_output = ""

app = Flask(__name__, template_folder='./html')
app.secret_key = os.urandom(24)

# 密碼從環境變數讀取，若沒設定則預設為 "admin"
PASSWORD = os.getenv("NHENTAI_PASSWORD", "admin")

# 下載相關設定
DOWNLOAD_PATH = "/nhentai"
DEFAULT_FORMAT = os.getenv("DEFAULT_FORMAT", "%a%t")

# 背景任務相關設定
MAX_TASK_HISTORY = 50
task_queue: "queue.Queue[dict]" = queue.Queue()
task_history = []
task_lock = threading.Lock()


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _summarize_result(output: str, returncode: int | None) -> tuple[str, str]:
    if "main: 🍻 All done." in output:
        return "success", "Download finished"
    if "cmd_parser: User-Agent saved" in output:
        return "success", "User-Agent saved"
    if "cmd_parser: Cookie saved" in output:
        return "success", "Cookie saved"
    if returncode == 0:
        return "warning", "Completed (check log)"
    return "failed", "Command failed"


STATUS_DISPLAY = {
    "queued": "Queued",
    "running": "Running",
    "success": "Success",
    "warning": "Completed (check log)",
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
            result = subprocess.run(task["command"], capture_output=True, text=True)
            output = (result.stdout or "") + (result.stderr or "")
            returncode = result.returncode
        except Exception as exc:  # noqa: BLE001
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


def run_nhentai_command(args: list[str], label: str | None = None) -> dict:
    return _enqueue_task(args, label)


@app.route('/')
def index():
    password_cookie = request.cookies.get("nhentai_auth")
    is_verified = password_cookie == "ok"
    return render_template("index.html", password=PASSWORD, verified=is_verified)

@app.route('/debug-log')
def debug_log():
    return latest_log_output, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route('/queue-status')
def queue_status():
    with task_lock:
        serialized = [_serialize_task(task) for task in task_history]

    pending = sum(1 for item in serialized if item["status"] in {"queued", "running"})
    return jsonify({
        "tasks": serialized,
        "pending": pending,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


@app.route('/verify-password', methods=['POST'])
def verify_password():
    password = request.form.get("password", "")
    if password == PASSWORD:
        resp = make_response(redirect(url_for("index")))
        resp.set_cookie("nhentai_auth", "ok", max_age=60*60*24*30)  # 有效期30天
        return resp
    else:
        flash("Wrong password", "error")
        return redirect(url_for("index"))

@app.route('/ua', methods=['POST'])
def ua():
    ua = request.form.get('ua', '').strip()
    if not ua or len(ua) > 200:
        flash("User-Agent error ❌", "error")
        return redirect(url_for('index'))
    task = run_nhentai_command(["nhentai", "--useragent", ua], label="Save User-Agent")
    flash(f"Task #{task['id']} queued to update User-Agent", "success")
    return redirect(url_for('index'))

@app.route('/cookies', methods=['POST'])
def cookies():
    ck = request.form.get('ck', '').strip()
    if not ck or len(ck) > 1000:
        flash("Cookie error ❌", "error")
        return redirect(url_for('index'))
    task = run_nhentai_command(["nhentai", "--cookie", ck], label="Save Cookie")
    flash(f"Task #{task['id']} queued to update Cookie", "success")
    return redirect(url_for('index'))

@app.route('/download', methods=['POST'])
def download():
    raw = request.form.get('id', '').strip()

    # Normalize whitespace: spaces / tabs / newlines 都會被 split() 處理
    parts = raw.split()

    # 驗證：每個 token 都必須是 6 位數字
    if not parts or any((not p.isdigit()) or len(p) != 6 for p in parts):
        flash("Invalid ID format ❌ (Use six-digit numbers separated by spaces)", "error")
        return redirect(url_for('index'))

    # 去重並保序
    seen = set()
    id_list = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            id_list.append(p)

    # （可選）限制一次最多 50 組
    if len(id_list) > 50:
        flash("Too many IDs at once ❌ (Max 50)", "error")
        return redirect(url_for('index'))

    queued = []
    for gid in id_list:
        command = [
            "nhentai", "--id", gid,
            "--page-all", "--download", "--delay", "1",
            "--cbz", "--format", DEFAULT_FORMAT,
            "--rm-origin-dir", "--output", DOWNLOAD_PATH
        ]
        task = run_nhentai_command(command, label=f"Download {gid}")
        queued.append(f"{gid} (#{task['id']})")

    if queued:
        flash(f"Queued {len(queued)} download task(s): {', '.join(queued)}", "success")
    else:
        flash("No tasks queued", "error")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=61234)

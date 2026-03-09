#!/usr/bin/env python3
"""Local web UI for running crawls and opening generated HTML reports."""

from __future__ import annotations

import json
import html
import mimetypes
import os
import re
import signal
import shlex
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Dict, List, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
TASKS_DB_PATH = BASE_DIR / "output" / ".web_tasks.json"
INTEGRATIONS_DB_PATH = BASE_DIR / "output" / ".web_integrations.json"
MAILER_DB_PATH = BASE_DIR / "output" / ".web_mailer.json"
DEFAULT_STATE = "auth_state_cookie.json"
DEFAULT_ZHIHU_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
DEFAULT_ZHIHU_COOKIE = os.environ.get("ZHIHU_DEFAULT_COOKIE", "").strip()
LOG_LIMIT = 1200
SCROLL_RE = re.compile(r"(?:滚动|Scroll)\s+(\d+)(?:/(\d+))?.*?共\s+(\d+)\s*条", re.IGNORECASE)
SCROLL_EN_RE = re.compile(r"Scroll\s+(\d+)(?:/(\d+))?:\s*\+\s*(\d+)\s+new,\s+total\s+(\d+)", re.IGNORECASE)
PAGE_EN_RE = re.compile(r"Page\s+(\d+)(?:/(\d+))?:\s*\+\s*(\d+)\s+new,\s+total\s+(\d+)", re.IGNORECASE)
TARGET_RE = re.compile(r"目标:\s*收集前\s*(\d+)\s*条")
SUCCESS_RE = re.compile(r"成功收集\s+(\d+)\s+条推文(?:（目标:\s*(\d+)条）)?")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RUN_DIR_RE = re.compile(r"(?:Run directory|运行目录):\s*(.+)")
FULLTEXT_RE = re.compile(r"\[FULLTEXT\]\s+(\d+)/(\d+)\s+(\d+)")

app = Flask(__name__)

TASKS: Dict[str, Dict] = {}
TASKS_LOCK = threading.Lock()
INTEGRATIONS: Dict[str, Dict] = {
    "bettafish": {
        "name": "BettaFish",
        "path": str((BASE_DIR / "integrations" / "BettaFish").resolve()),
        "python_cmd": "python",
        "host": "127.0.0.1",
        "port": 5000,
        "status": "idle",
        "message": "",
        "error": "",
        "logs": [],
        "process": None,
        "pid": None,
        "updated_at": "",
    }
}
INTEGRATIONS_LOCK = threading.Lock()
MAILER: Dict[str, object] = {
    "smtp_host": "mail.alumni.sjtu.edu.cn",
    "smtp_port": 465,
    "smtp_security": "ssl",
    "username": "",
    "password": "",
    "sender_email": "",
    "sender_name": "SJTU Alumni Mail",
    "updated_at": "",
}
MAILER_LOCK = threading.Lock()


def sanitize_task_params(task_type: str, params: Dict) -> Dict:
    payload = dict(params or {})
    if task_type in {"zhihu_question", "zhihu_search"} and payload.get("cookie"):
        payload["cookie"] = "[hidden]"
    return payload


def task_to_disk_record(task: Dict) -> Dict:
    return {
        "id": task["id"],
        "type": task["type"],
        "params": sanitize_task_params(task["type"], task["params"]),
        "status": task["status"],
        "stage": task["stage"],
        "progress": task["progress"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "logs": task["logs"],
        "message": task["message"],
        "error": task["error"],
        "result_dir": task["result_dir"],
        "cancel_requested": task.get("cancel_requested", False),
        "target_items": task.get("target_items", 0),
        "collected_items": task.get("collected_items", 0),
        "current_scroll": task.get("current_scroll", 0),
        "max_scrolls": task.get("max_scrolls", 0),
        "last_new_items": task.get("last_new_items", 0),
        "fulltext_total": task.get("fulltext_total", 0),
        "fulltext_processed": task.get("fulltext_processed", 0),
        "fulltext_hydrated": task.get("fulltext_hydrated", 0),
        "fulltext_failed": task.get("fulltext_failed", 0),
    }


def save_tasks_to_disk() -> None:
    with TASKS_LOCK:
        payload = [task_to_disk_record(task) for task in TASKS.values()]
    TASKS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_DB_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tasks_from_disk() -> None:
    if not TASKS_DB_PATH.exists():
        return
    try:
        records = json.loads(TASKS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with TASKS_LOCK:
        for record in records:
            task = {
                "id": record["id"],
                "type": record.get("type", "keyword"),
                "params": record.get("params", {}),
                "status": record.get("status", "failed"),
                "stage": record.get("stage", ""),
                "progress": int(record.get("progress", 0) or 0),
                "created_at": record.get("created_at", now),
                "updated_at": record.get("updated_at", now),
                "logs": trim_logs(list(record.get("logs", []))),
                "message": record.get("message", ""),
                "error": record.get("error", ""),
                "result_dir": record.get("result_dir", ""),
                "process": None,
                "pid": None,
                "cancel_requested": bool(record.get("cancel_requested", False)),
                "target_items": int(record.get("target_items", 0) or 0),
                "collected_items": int(record.get("collected_items", 0) or 0),
                "current_scroll": int(record.get("current_scroll", 0) or 0),
                "max_scrolls": int(record.get("max_scrolls", 0) or 0),
                "last_new_items": int(record.get("last_new_items", 0) or 0),
                "fulltext_total": int(record.get("fulltext_total", 0) or 0),
                "fulltext_processed": int(record.get("fulltext_processed", 0) or 0),
                "fulltext_hydrated": int(record.get("fulltext_hydrated", 0) or 0),
                "fulltext_failed": int(record.get("fulltext_failed", 0) or 0),
            }
            if task["status"] in {"queued", "running", "cancelling"}:
                task["status"] = "interrupted"
                task["stage"] = "服务重启前中断"
                if not task["error"]:
                    task["error"] = "服务重启前任务仍在运行，当前仅保留历史状态。"
            TASKS[task["id"]] = task


def integration_to_disk_record(item: Dict) -> Dict:
    return {
        "name": item["name"],
        "path": item.get("path", ""),
        "python_cmd": item.get("python_cmd", "python"),
        "host": item.get("host", "127.0.0.1"),
        "port": int(item.get("port", 5000) or 5000),
        "status": item.get("status", "idle"),
        "message": item.get("message", ""),
        "error": item.get("error", ""),
        "logs": trim_logs(list(item.get("logs", []))),
        "updated_at": item.get("updated_at", ""),
    }


def save_integrations_to_disk() -> None:
    with INTEGRATIONS_LOCK:
        payload = {name: integration_to_disk_record(item) for name, item in INTEGRATIONS.items()}
    INTEGRATIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTEGRATIONS_DB_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_integrations_from_disk() -> None:
    if not INTEGRATIONS_DB_PATH.exists():
        return
    try:
        payload = json.loads(INTEGRATIONS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    with INTEGRATIONS_LOCK:
        for name, record in payload.items():
            if name not in INTEGRATIONS:
                continue
            INTEGRATIONS[name].update(
                {
                    "path": record.get("path", INTEGRATIONS[name]["path"]),
                    "python_cmd": record.get("python_cmd", INTEGRATIONS[name]["python_cmd"]),
                    "host": record.get("host", INTEGRATIONS[name]["host"]),
                    "port": int(record.get("port", INTEGRATIONS[name]["port"]) or INTEGRATIONS[name]["port"]),
                    "status": record.get("status", "idle"),
                    "message": record.get("message", ""),
                    "error": record.get("error", ""),
                    "logs": trim_logs(list(record.get("logs", []))),
                    "updated_at": record.get("updated_at", ""),
                    "process": None,
                    "pid": None,
                }
            )
            if INTEGRATIONS[name]["status"] in {"running", "starting", "stopping"}:
                INTEGRATIONS[name]["status"] = "stopped"
                INTEGRATIONS[name]["message"] = "服务重启后未保留原进程，仅恢复历史配置。"


def integration_log(name: str, line: str) -> None:
    clean = line.rstrip("\n")
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS.get(name)
        if not item:
            return
        item["logs"].append(clean)
        item["logs"] = trim_logs(item["logs"])
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_integrations_to_disk()


def update_integration(name: str, **changes) -> None:
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS.get(name)
        if not item:
            return
        item.update(changes)
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_integrations_to_disk()


def integration_url(name: str) -> str:
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS[name]
        return f"http://{item['host']}:{item['port']}"


def is_service_online(url: str) -> bool:
    try:
        with urlrequest.urlopen(f"{url}/api/status", timeout=1.2) as resp:
            return 200 <= resp.status < 500
    except Exception:
        try:
            with urlrequest.urlopen(url, timeout=1.2) as resp:
                return 200 <= resp.status < 500
        except Exception:
            return False


def integration_payload(name: str) -> Dict:
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS.get(name)
        if not item:
            return {"error": "integration not found"}
        process = item.get("process")
        running = bool(process and process.poll() is None)
        url = f"http://{item['host']}:{item['port']}"
        online = is_service_online(url)
        status = item["status"]
        error_text = item.get("error", "")
        message_text = item.get("message", "")
        if online and not running and status in {"idle", "stopped"}:
            status = "online"
        if online and not running:
            status = "online"
            error_text = ""
            if not message_text:
                message_text = "BettaFish 已在线，可直接打开。"
        if running:
            status = "running"
        return {
            "name": item["name"],
            "path": item.get("path", ""),
            "python_cmd": item.get("python_cmd", "python"),
            "host": item.get("host", "127.0.0.1"),
            "port": int(item.get("port", 5000) or 5000),
            "status": status,
            "message": message_text,
            "error": error_text,
            "logs": "\n".join(item.get("logs", [])),
            "url": url,
            "online": online,
            "updated_at": item.get("updated_at", ""),
            "pid": item.get("pid"),
        }


def _bettafish_log_pump(process: subprocess.Popen, name: str) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        integration_log(name, line)
    code = process.wait()
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS.get(name)
        if not item:
            return
        item["process"] = None
        item["pid"] = None
        if item.get("status") == "stopping":
            item["status"] = "stopped"
            item["message"] = "BettaFish 已停止。"
        elif code == 0:
            item["status"] = "stopped"
            item["message"] = "BettaFish 已退出。"
        else:
            item["status"] = "error"
            item["error"] = f"BettaFish 退出码 {code}"
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_integrations_to_disk()


def start_bettafish_service() -> Dict:
    current = integration_payload("bettafish")
    if current.get("online"):
        return current
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS["bettafish"]
        process = item.get("process")
        if process and process.poll() is None:
            return integration_payload("bettafish")
        app_path = Path(item["path"]).expanduser().resolve()
        python_cmd = item.get("python_cmd", "python")
        host = item.get("host", "127.0.0.1")
        port = int(item.get("port", 5000) or 5000)
    app_file = app_path / "app.py"
    if not app_file.exists():
        raise RuntimeError("BettaFish 路径无效，未找到 app.py。")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HOST"] = host
    env["PORT"] = str(port)
    cmd = shlex.split(python_cmd) + ["app.py"]
    process = subprocess.Popen(
        cmd,
        cwd=app_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    update_integration("bettafish", process=process, pid=process.pid, status="starting", error="", message="BettaFish 启动中...")
    integration_log("bettafish", f"[SYSTEM] Starting BettaFish with command: {' '.join(cmd)}")
    thread = threading.Thread(target=_bettafish_log_pump, args=(process, "bettafish"), daemon=True)
    thread.start()
    return integration_payload("bettafish")


def stop_bettafish_service() -> Dict:
    should_save = False
    with INTEGRATIONS_LOCK:
        item = INTEGRATIONS["bettafish"]
        process = item.get("process")
        if not process or process.poll() is not None:
            item["status"] = "stopped"
            item["message"] = "BettaFish 未在当前控制台中运行。"
            item["process"] = None
            item["pid"] = None
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            should_save = True
        else:
            item["status"] = "stopping"
            pid = process.pid
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            should_save = True
    if should_save:
        save_integrations_to_disk()
    if not process or process.poll() is not None:
        return integration_payload("bettafish")
    integration_log("bettafish", "[SYSTEM] 正在请求停止 BettaFish ...")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return integration_payload("bettafish")


def mailer_to_disk_record() -> Dict:
    with MAILER_LOCK:
        return {
            "smtp_host": str(MAILER.get("smtp_host", "")).strip(),
            "smtp_port": int(MAILER.get("smtp_port", 587) or 587),
            "smtp_security": str(MAILER.get("smtp_security", "starttls")).strip() or "starttls",
            "username": str(MAILER.get("username", "")).strip(),
            "password": str(MAILER.get("password", "")),
            "sender_email": str(MAILER.get("sender_email", "")).strip(),
            "sender_name": str(MAILER.get("sender_name", "")).strip(),
            "updated_at": str(MAILER.get("updated_at", "")),
        }


def save_mailer_to_disk() -> None:
    MAILER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAILER_DB_PATH.write_text(json.dumps(mailer_to_disk_record(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_mailer_from_disk() -> None:
    if not MAILER_DB_PATH.exists():
        return
    try:
        payload = json.loads(MAILER_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    with MAILER_LOCK:
        MAILER.update(
            {
                "smtp_host": str(payload.get("smtp_host", "")).strip(),
                "smtp_port": int(payload.get("smtp_port", 587) or 587),
                "smtp_security": str(payload.get("smtp_security", "starttls")).strip() or "starttls",
                "username": str(payload.get("username", "")).strip(),
                "password": str(payload.get("password", "")),
                "sender_email": str(payload.get("sender_email", "")).strip(),
                "sender_name": str(payload.get("sender_name", "")).strip(),
                "updated_at": str(payload.get("updated_at", "")),
            }
        )


def mailer_payload(include_secret: bool = False) -> Dict:
    with MAILER_LOCK:
        payload = {
            "smtp_host": str(MAILER.get("smtp_host", "")).strip(),
            "smtp_port": int(MAILER.get("smtp_port", 587) or 587),
            "smtp_security": str(MAILER.get("smtp_security", "starttls")).strip() or "starttls",
            "username": str(MAILER.get("username", "")).strip(),
            "sender_email": str(MAILER.get("sender_email", "")).strip(),
            "sender_name": str(MAILER.get("sender_name", "")).strip(),
            "updated_at": str(MAILER.get("updated_at", "")),
            "has_password": bool(MAILER.get("password")),
        }
        if include_secret:
            payload["password"] = str(MAILER.get("password", ""))
        return payload


def update_mailer(payload: Dict) -> Dict:
    smtp_host = str(payload.get("smtp_host", "")).strip()
    username = str(payload.get("username", "")).strip()
    sender_email = str(payload.get("sender_email", "")).strip()
    sender_name = str(payload.get("sender_name", "")).strip()
    smtp_security = str(payload.get("smtp_security", "starttls")).strip().lower() or "starttls"
    raw_port = str(payload.get("smtp_port", "587")).strip() or "587"
    if smtp_security not in {"starttls", "ssl", "none"}:
        raise ValueError("SMTP 加密方式只支持 starttls / ssl / none。")
    try:
        smtp_port = int(raw_port)
    except ValueError as exc:
        raise ValueError("SMTP 端口必须是整数。") from exc
    password_in_payload = "password" in payload
    password = str(payload.get("password", "")) if password_in_payload else None
    with MAILER_LOCK:
        MAILER["smtp_host"] = smtp_host
        MAILER["smtp_port"] = smtp_port
        MAILER["smtp_security"] = smtp_security
        MAILER["username"] = username
        MAILER["sender_email"] = sender_email
        MAILER["sender_name"] = sender_name
        if password is not None and password != "":
            MAILER["password"] = password
        elif password == "":
            MAILER["password"] = MAILER.get("password", "")
        MAILER["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_mailer_to_disk()
    return mailer_payload()


def parse_recipients(raw_text: str) -> List[str]:
    seen = set()
    recipients: List[str] = []
    parts = re.split(r"[\s,;，；]+", raw_text.strip())
    for part in parts:
        email = part.strip()
        if not email:
            continue
        if not EMAIL_RE.match(email):
            raise ValueError(f"邮箱格式不合法: {email}")
        lowered = email.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        recipients.append(email)
    return recipients


def run_report_files(run_dir: Path) -> List[Dict]:
    files = [
        ("价值排序页", run_dir / "usefulness_ranking.html"),
        ("深度文章页", run_dir / "article.html"),
        ("摘要页", run_dir / "summary.html"),
        ("知乎回答全文", run_dir / "all_answers.md"),
        ("知乎搜索全文", run_dir / "all_results.md"),
        ("评分 JSON", run_dir / "usefulness_ranking.json"),
        ("结果 JSON", run_dir / "results.json"),
        ("阶段1结果 JSON", run_dir / "results_stage1.json"),
        ("详情失败记录", run_dir / "failed_details.json"),
        ("全文补全进度", run_dir / "fulltext_progress.json"),
        ("结果 CSV", run_dir / "results.csv"),
        ("摘要 Markdown", run_dir / "summary.md"),
        ("详细报告", run_dir / "detailed_report.html"),
        ("详细报告 Markdown", run_dir / "detailed_report.md"),
    ]
    items: List[Dict] = []
    for label, path in files:
        if path.exists():
            rel = path.relative_to(BASE_DIR).as_posix()
            items.append(
                {
                    "label": label,
                    "name": path.name,
                    "path": str(path),
                    "relpath": rel,
                    "url": f"/files/{rel}",
                }
            )
    return items


def resolve_run_dir(run_name: str) -> Path:
    clean_name = (run_name or "").strip()
    if not clean_name:
        raise ValueError("请选择要发送的输出目录。")
    target = (OUTPUT_DIR / clean_name).resolve()
    if OUTPUT_DIR not in target.parents:
        raise ValueError("输出目录路径非法。")
    if not target.exists() or not target.is_dir():
        raise ValueError("输出目录不存在。")
    return target


def build_mail_html(body: str, run_name: str, attachment_names: List[str]) -> str:
    safe_body = "<br>".join(
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for line in body.splitlines()
    )
    attachment_html = "".join(
        f"<li>{name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</li>"
        for name in attachment_names
    )
    return (
        "<html><body style=\"font-family:Arial,'PingFang SC',sans-serif;color:#1f2937;line-height:1.7;\">"
        f"<div>{safe_body or ' '}</div>"
        f"<hr style=\"border:none;border-top:1px solid #e5e7eb;margin:20px 0;\">"
        f"<p style=\"margin:0 0 8px;color:#6b7280;\">来自本地控制台的批量邮件投递</p>"
        f"<p style=\"margin:0 0 8px;\"><strong>输出目录：</strong>{run_name}</p>"
        f"<p style=\"margin:0 0 8px;\"><strong>附件数量：</strong>{len(attachment_names)}</p>"
        f"<ul style=\"margin:8px 0 0 20px;\">{attachment_html}</ul>"
        "</body></html>"
    )


def send_one_email(
    smtp_settings: Dict,
    recipient: str,
    subject: str,
    body: str,
    run_name: str,
    attachments: List[Path],
) -> None:
    msg = EmailMessage()
    sender_email = smtp_settings["sender_email"]
    sender_name = smtp_settings.get("sender_name", "")
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body or " ")
    msg.add_alternative(build_mail_html(body, run_name, [path.name for path in attachments]), subtype="html")

    for attachment in attachments:
        mime_type, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = ("application", "octet-stream")
        if mime_type and "/" in mime_type:
            maintype, subtype = mime_type.split("/", 1)
        msg.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )

    context = ssl.create_default_context()
    security = smtp_settings["smtp_security"]
    host = smtp_settings["smtp_host"]
    port = smtp_settings["smtp_port"]
    username = smtp_settings["username"]
    password = smtp_settings["password"]
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            if username:
                server.login(username, password)
            server.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if username:
            server.login(username, password)
        server.send_message(msg)


def run_email_job(task_id: str, params: Dict) -> None:
    with MAILER_LOCK:
        smtp_settings = {
            "smtp_host": str(MAILER.get("smtp_host", "")).strip(),
            "smtp_port": int(MAILER.get("smtp_port", 587) or 587),
            "smtp_security": str(MAILER.get("smtp_security", "starttls")).strip() or "starttls",
            "username": str(MAILER.get("username", "")).strip(),
            "password": str(MAILER.get("password", "")),
            "sender_email": str(MAILER.get("sender_email", "")).strip(),
            "sender_name": str(MAILER.get("sender_name", "")).strip(),
        }
    required = {
        "smtp_host": "SMTP Host",
        "sender_email": "发件邮箱",
    }
    for key, label in required.items():
        if not smtp_settings.get(key):
            raise ValueError(f"{label} 未配置。")
    if smtp_settings["username"] and not smtp_settings["password"]:
        raise ValueError("已填写 SMTP 用户名，但密码为空。")

    recipients = parse_recipients(str(params.get("recipients", "")))
    if not recipients:
        raise ValueError("请至少填写一个收件邮箱。")
    subject = str(params.get("subject", "")).strip()
    if not subject:
        raise ValueError("邮件标题不能为空。")
    body = str(params.get("body", "")).strip()
    run_dir = resolve_run_dir(str(params.get("run_name", "")))
    all_files = {item["name"]: Path(item["path"]) for item in run_report_files(run_dir)}
    selected_names = [str(item).strip() for item in params.get("attachments", []) if str(item).strip()]
    attachments: List[Path] = []
    for name in selected_names:
        if name not in all_files:
            raise ValueError(f"未找到附件: {name}")
        attachments.append(all_files[name])

    update_task(task_id, stage="正在连接 SMTP", progress=8, result_dir=str(run_dir))
    append_log(task_id, f"[SYSTEM] 目标收件人数: {len(recipients)}")
    append_log(task_id, f"[SYSTEM] 发送目录: {run_dir.name}")
    if attachments:
        append_log(task_id, f"[SYSTEM] 附件: {', '.join(path.name for path in attachments)}")
    else:
        append_log(task_id, "[SYSTEM] 本次邮件不附带附件。")
    failures: List[str] = []
    total = len(recipients)
    for index, recipient in enumerate(recipients, start=1):
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            cancelled = bool(task and task.get("cancel_requested"))
        if cancelled:
            raise RuntimeError("邮件任务已停止。")
        update_task(task_id, stage=f"正在发送第 {index}/{total} 封", progress=min(96, 12 + int(index / total * 80)))
        append_log(task_id, f"[SEND] {recipient}")
        try:
            send_one_email(smtp_settings, recipient, subject, body, run_dir.name, attachments)
            append_log(task_id, f"[OK] {recipient}")
        except Exception as exc:
            failures.append(f"{recipient}: {exc}")
            append_log(task_id, f"[ERROR] {recipient}: {exc}")
    if failures:
        raise RuntimeError("部分邮件发送失败。\n" + "\n".join(failures))
    update_task(
        task_id,
        message=f"已成功发送 {total} 封邮件。",
        stage="已完成",
        progress=100,
        result_dir=str(run_dir),
    )


def list_run_dirs(limit: int = 12) -> List[Path]:
    if not OUTPUT_DIR.exists():
        return []
    dirs = [p for p in OUTPUT_DIR.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[:limit]


def resolve_report_links(run_dir: Path) -> List[Tuple[str, str]]:
    return [(item["label"], item["url"]) for item in run_report_files(run_dir)]


def detect_newest_dir(before: set[str]) -> Path | None:
    after = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    created = sorted(after - before)
    if created:
        return OUTPUT_DIR / created[-1]
    candidates = list_run_dirs(limit=1)
    return candidates[0] if candidates else None


def recent_runs_payload() -> List[Dict]:
    payload = []
    for run_dir in list_run_dirs():
        files = run_report_files(run_dir)
        payload.append(
            {
                "name": run_dir.name,
                "updated_at": datetime.fromtimestamp(run_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "links": [{"label": item["label"], "url": item["url"]} for item in files],
                "files": [{"label": item["label"], "name": item["name"]} for item in files],
            }
        )
    return payload


def trim_logs(lines: List[str]) -> List[str]:
    if len(lines) <= LOG_LIMIT:
        return lines
    return ["...[日志过长，已截断较早内容]..."] + lines[-LOG_LIMIT:]


def read_fulltext_progress(run_dir: Path | None) -> Dict[str, int]:
    if not run_dir or not run_dir.exists():
        return {"fulltext_total": 0, "fulltext_processed": 0, "fulltext_hydrated": 0, "fulltext_failed": 0}
    path = run_dir / "fulltext_progress.json"
    if not path.exists():
        return {"fulltext_total": 0, "fulltext_processed": 0, "fulltext_hydrated": 0, "fulltext_failed": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"fulltext_total": 0, "fulltext_processed": 0, "fulltext_hydrated": 0, "fulltext_failed": 0}
    return {
        "fulltext_total": int(payload.get("total", 0) or 0),
        "fulltext_processed": int(payload.get("processed", 0) or 0),
        "fulltext_hydrated": int(payload.get("hydrated", 0) or 0),
        "fulltext_failed": int(payload.get("failed", 0) or 0),
    }


def append_log(task_id: str, line: str) -> None:
    clean = line.rstrip("\n")
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        task["logs"].append(clean)
        task["logs"] = trim_logs(task["logs"])
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        infer_progress(task, clean)
    save_tasks_to_disk()


def infer_progress(task: Dict, line: str) -> None:
    lowered = line.lower()
    run_dir_match = RUN_DIR_RE.search(line)
    if run_dir_match:
        try:
            run_dir = Path(run_dir_match.group(1).strip()).resolve()
            if OUTPUT_DIR in run_dir.parents:
                task["result_dir"] = str(run_dir)
        except Exception:
            pass

    target_match = TARGET_RE.search(line)
    if target_match:
        task["target_items"] = int(target_match.group(1))

    scroll_match = SCROLL_RE.search(line)
    if scroll_match:
        current_scroll = int(scroll_match.group(1))
        max_scrolls = int(scroll_match.group(2) or 0)
        collected = int(scroll_match.group(3))
        new_match = re.search(r"\+\s*(\d+)\s*条", line)
        task["current_scroll"] = current_scroll
        task["max_scrolls"] = max_scrolls
        task["collected_items"] = collected
        task["last_new_items"] = int(new_match.group(1)) if new_match else 0
        task["stage"] = "正在抓取内容"
        if task.get("target_items"):
            ratio = min(task["collected_items"] / max(task["target_items"], 1), 1.0)
            task["progress"] = max(task["progress"], min(78, int(12 + ratio * 66)))
        else:
            task["progress"] = min(78, max(task["progress"], 12 + current_scroll))

    scroll_en_match = SCROLL_EN_RE.search(line)
    if scroll_en_match:
        current_scroll = int(scroll_en_match.group(1))
        max_scrolls = int(scroll_en_match.group(2) or 0)
        new_items = int(scroll_en_match.group(3))
        collected = int(scroll_en_match.group(4))
        task["current_scroll"] = current_scroll
        task["max_scrolls"] = max_scrolls
        task["collected_items"] = collected
        task["last_new_items"] = new_items
        task["stage"] = "正在抓取内容"
        task["progress"] = min(78, max(task["progress"], 12 + current_scroll))

    page_match = PAGE_EN_RE.search(line)
    if page_match:
        current_scroll = int(page_match.group(1))
        max_scrolls = int(page_match.group(2) or 0)
        new_items = int(page_match.group(3))
        collected = int(page_match.group(4))
        task["current_scroll"] = current_scroll
        task["max_scrolls"] = max_scrolls
        task["collected_items"] = collected
        task["last_new_items"] = new_items
        task["stage"] = "正在抓取列表"
        task["progress"] = min(78, max(task["progress"], 12 + current_scroll))

    success_match = SUCCESS_RE.search(line)
    if success_match:
        task["collected_items"] = int(success_match.group(1))
        if success_match.group(2):
            task["target_items"] = int(success_match.group(2))

    fulltext_match = FULLTEXT_RE.search(line)
    if fulltext_match:
        processed = int(fulltext_match.group(1))
        total = int(fulltext_match.group(2))
        task["fulltext_processed"] = processed
        task["fulltext_total"] = total
        task["stage"] = "正在补全文"
        if total > 0:
            task["progress"] = max(task["progress"], min(96, 82 + int(processed / total * 14)))

    if "读取 " in line and "results.json" in line:
        task["stage"] = "正在评分排序"
        task["progress"] = max(task["progress"], 88)
    elif "已生成排名页面" in line or "ranking" in lowered and "html" in lowered:
        task["stage"] = "正在整理输出"
        task["progress"] = max(task["progress"], 96)
    elif "成功收集" in line or "完成！已收集" in line:
        task["stage"] = "抓取完成，准备评分"
        task["progress"] = max(task["progress"], 82)
    elif "stage 2: hydrating full text" in lowered or "开始第二阶段：逐条补全推文全文" in line:
        task["stage"] = "正在补全文"
        task["progress"] = max(task["progress"], 82)
    elif "search url" in lowered or "搜索关键词" in line:
        task["stage"] = "正在打开搜索页"
        task["progress"] = max(task["progress"], 8)
    elif "获取个人账号关注的所有人的最新500条动态" in line:
        task["stage"] = "正在打开关注流"
        task["progress"] = max(task["progress"], 8)
    elif "question title:" in lowered:
        task["stage"] = "正在收集回答链接"
        task["progress"] = max(task["progress"], 10)
    elif "discovered" in lowered and "answer links" in lowered:
        task["stage"] = "正在逐条抓取回答全文"
        task["progress"] = max(task["progress"], 26)
    elif "[answer]" in lowered:
        task["stage"] = "正在逐条抓取回答全文"
        task["progress"] = max(task["progress"], 28)
    elif "开始第二阶段：逐条补全知乎全文" in line:
        task["stage"] = "正在补全文"
        task["progress"] = max(task["progress"], 82)
    elif "[detail]" in lowered:
        task["stage"] = "正在逐条抓取详情全文"
        task["progress"] = max(task["progress"], 84)


def update_task(task_id: str, **changes) -> None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_tasks_to_disk()


def list_tasks_payload(limit: int = 24) -> List[Dict]:
    with TASKS_LOCK:
        tasks = list(TASKS.values())
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    payload = []
    for task in tasks[:limit]:
        run_dir = Path(task["result_dir"]) if task.get("result_dir") else None
        fulltext = read_fulltext_progress(run_dir)
        payload.append(
            {
                "id": task["id"],
                "type": task["type"],
                "status": task["status"],
                "stage": task["stage"],
                "progress": task["progress"],
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "message": task["message"],
                "error": task["error"],
                "result_dir": run_dir.name if run_dir else "",
                "cancel_requested": task.get("cancel_requested", False),
                "target_items": task.get("target_items", 0),
                "collected_items": task.get("collected_items", 0),
                "current_scroll": task.get("current_scroll", 0),
                "max_scrolls": task.get("max_scrolls", 0),
                "last_new_items": task.get("last_new_items", 0),
                "fulltext_total": fulltext["fulltext_total"] or task.get("fulltext_total", 0),
                "fulltext_processed": fulltext["fulltext_processed"] or task.get("fulltext_processed", 0),
                "fulltext_hydrated": fulltext["fulltext_hydrated"] or task.get("fulltext_hydrated", 0),
                "fulltext_failed": fulltext["fulltext_failed"] or task.get("fulltext_failed", 0),
            }
        )
    return payload


def run_command_stream(task_id: str, args: List[str], stage: str, progress_floor: int) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    update_task(task_id, stage=stage, progress=max(progress_floor, 1))
    process = subprocess.Popen(
        args,
        cwd=BASE_DIR,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    update_task(task_id, process=process, pid=process.pid)
    assert process.stdout is not None
    for line in process.stdout:
        append_log(task_id, line)
    code = process.wait()
    update_task(task_id, process=None, pid=None)
    return code


def finalize_partial_outputs(task_id: str, task_type: str) -> bool:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        run_dir = Path(task["result_dir"]) if task and task.get("result_dir") else None
    if not run_dir or not run_dir.exists():
        return False
    if task_type not in {"keyword", "following", "user_timeline"}:
        return False
    results_json = run_dir / "results.json"
    if not results_json.exists():
        return False

    update_task(task_id, stage="正在整理已抓取结果", progress=96, result_dir=str(run_dir))
    append_log(task_id, f"[SYSTEM] 正在基于已抓取内容生成汇总文件：{run_dir.name}")
    proc = subprocess.run(
        [sys.executable, "rank_usefulness.py", "--input", str(run_dir)],
        cwd=BASE_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            append_log(task_id, line)
    if proc.returncode != 0:
        append_log(task_id, "[SYSTEM] 已停止任务，但补充生成排序页面失败。")
        return False
    append_log(task_id, "[SYSTEM] 已停止任务，并生成当前结果的汇总页面。")
    return True


def terminate_task_process(task_id: str) -> bool:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return False
        process = task.get("process")
        if not process or process.poll() is not None:
            task["cancel_requested"] = True
            return False
        task["cancel_requested"] = True
        task["status"] = "cancelling"
        task["stage"] = "正在停止任务"
        pid = process.pid
    append_log(task_id, "[SYSTEM] 正在请求停止任务...")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + 5
    while time.time() < deadline:
        if process.poll() is not None:
            update_task(task_id, process=None, pid=None)
            append_log(task_id, "[SYSTEM] 任务已停止。")
            return True
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
        append_log(task_id, "[SYSTEM] 任务未及时退出，已强制结束。")
    except ProcessLookupError:
        pass
    update_task(task_id, process=None, pid=None)
    return True


def create_task(task_type: str, params: Dict) -> str:
    task_id = uuid.uuid4().hex[:12]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task = {
        "id": task_id,
        "type": task_type,
        "params": params,
        "status": "queued",
        "stage": "等待执行",
        "progress": 0,
        "created_at": now,
        "updated_at": now,
        "logs": [],
        "message": "",
        "error": "",
        "result_dir": "",
        "process": None,
        "pid": None,
        "cancel_requested": False,
        "target_items": 0,
        "collected_items": 0,
        "current_scroll": 0,
        "max_scrolls": 0,
        "last_new_items": 0,
        "fulltext_total": 0,
        "fulltext_processed": 0,
        "fulltext_hydrated": 0,
        "fulltext_failed": 0,
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
    save_tasks_to_disk()
    return task_id


def task_payload(task_id: str) -> Dict:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return {"error": "task not found"}
        run_dir = Path(task["result_dir"]) if task.get("result_dir") else None
        result_links = []
        if run_dir and run_dir.exists():
            result_links = [
                {"label": label, "url": url}
                for label, url in resolve_report_links(run_dir)
            ]
        fulltext = read_fulltext_progress(run_dir)
        return {
            "id": task["id"],
            "type": task["type"],
            "status": task["status"],
            "stage": task["stage"],
            "progress": task["progress"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "message": task["message"],
            "error": task["error"],
            "logs": "\n".join(task["logs"]),
            "result_dir": run_dir.name if run_dir else "",
            "result_links": result_links,
            "cancel_requested": task.get("cancel_requested", False),
            "target_items": task.get("target_items", 0),
            "collected_items": task.get("collected_items", 0),
            "current_scroll": task.get("current_scroll", 0),
            "max_scrolls": task.get("max_scrolls", 0),
            "last_new_items": task.get("last_new_items", 0),
            "fulltext_total": fulltext["fulltext_total"] or task.get("fulltext_total", 0),
            "fulltext_processed": fulltext["fulltext_processed"] or task.get("fulltext_processed", 0),
            "fulltext_hydrated": fulltext["fulltext_hydrated"] or task.get("fulltext_hydrated", 0),
            "fulltext_failed": fulltext["fulltext_failed"] or task.get("fulltext_failed", 0),
        }


def run_keyword_job(task_id: str, keyword: str, lang: str, state: str, headless: bool, hydrate_fulltext: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "search_keyword_500.py", "--keyword", keyword, "--state", state]
    if lang:
        cmd.extend(["--lang", lang])
    if not hydrate_fulltext:
        cmd.append("--skip-fulltext")
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取关键词结果", 5)
    if code != 0:
        raise RuntimeError("关键词抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("关键词抓取完成，但未找到输出目录。")

    update_task(task_id, result_dir=str(run_dir), stage="正在生成排序 HTML", progress=86)
    code = run_command_stream(task_id, [sys.executable, "rank_usefulness.py", "--input", str(run_dir)], "正在生成排序 HTML", 88)
    if code != 0:
        raise RuntimeError("关键词评分失败，请检查日志。")
    update_task(
        task_id,
        result_dir=str(run_dir),
        message=f"关键词 “{keyword}” 已处理完成。",
        stage="已完成",
        progress=100,
    )


def run_following_job(task_id: str, state: str, headless: bool, hydrate_fulltext: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "crawl_following_timeline_500.py", "--state", state]
    if not hydrate_fulltext:
        cmd.append("--skip-fulltext")
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取关注流", 5)
    if code != 0:
        raise RuntimeError("关注流抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("关注流抓取完成，但未找到输出目录。")

    update_task(task_id, result_dir=str(run_dir), stage="正在生成排序 HTML", progress=86)
    code = run_command_stream(task_id, [sys.executable, "rank_usefulness.py", "--input", str(run_dir)], "正在生成排序 HTML", 88)
    if code != 0:
        raise RuntimeError("关注流评分失败，请检查日志。")
    update_task(
        task_id,
        result_dir=str(run_dir),
        message="关注流抓取与 HTML 生成完成。",
        stage="已完成",
        progress=100,
    )


def run_user_timeline_job(task_id: str, user_url: str, state: str, headless: bool, hydrate_fulltext: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "crawl_user_timeline.py", "--user-url", user_url, "--state", state, "--max-items", "0"]
    if not hydrate_fulltext:
        cmd.append("--skip-fulltext")
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取博主历史推文", 5)
    if code != 0:
        raise RuntimeError("博主历史推文抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("历史推文抓取完成，但未找到输出目录。")

    update_task(task_id, result_dir=str(run_dir), stage="正在生成排序 HTML", progress=86)
    code = run_command_stream(task_id, [sys.executable, "rank_usefulness.py", "--input", str(run_dir)], "正在生成排序 HTML", 88)
    if code != 0:
        raise RuntimeError("历史推文评分失败，请检查日志。")
    update_task(
        task_id,
        result_dir=str(run_dir),
        message=f"博主历史推文抓取完成：{user_url}",
        stage="已完成",
        progress=100,
    )


def run_user_following_job(task_id: str, user_url: str, state: str, headless: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "crawl_user_following.py", "--user-url", user_url, "--state", state, "--max-items", "0"]
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取博主关注列表", 5)
    if code != 0:
        raise RuntimeError("博主关注列表抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("关注列表抓取完成，但未找到输出目录。")

    update_task(
        task_id,
        result_dir=str(run_dir),
        message=f"博主关注列表抓取完成：{user_url}",
        stage="已完成",
        progress=100,
    )


def run_zhihu_question_job(task_id: str, question_url: str, cookie: str, user_agent: str, headless: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "zhihu_question_answers.py", "--question-url", question_url, "--cookie", cookie]
    if user_agent:
        cmd.extend(["--user-agent", user_agent])
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取知乎问题回答", 5)
    if code != 0:
        raise RuntimeError("知乎回答抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("知乎回答抓取完成，但未找到输出目录。")

    update_task(
        task_id,
        result_dir=str(run_dir),
        message=f"知乎问题回答抓取完成：{question_url}",
        stage="已完成",
        progress=100,
    )


def run_zhihu_search_job(task_id: str, keyword: str, cookie: str, user_agent: str, headless: bool) -> None:
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_dir()} if OUTPUT_DIR.exists() else set()
    cmd = [sys.executable, "zhihu_search_keyword_500.py", "--keyword", keyword, "--cookie", cookie]
    if user_agent:
        cmd.extend(["--user-agent", user_agent])
    if headless:
        cmd.append("--headless")
    code = run_command_stream(task_id, cmd, "正在抓取知乎搜索结果", 5)
    if code != 0:
        raise RuntimeError("知乎搜索抓取失败，请检查日志。")

    run_dir = detect_newest_dir(before)
    if run_dir is None:
        raise RuntimeError("知乎搜索抓取完成，但未找到输出目录。")

    update_task(
        task_id,
        result_dir=str(run_dir),
        message=f"知乎搜索抓取完成：{keyword}",
        stage="已完成",
        progress=100,
    )


def worker(task_id: str) -> None:
    update_task(task_id, status="running", stage="准备启动", progress=2)
    try:
        with TASKS_LOCK:
            task = dict(TASKS[task_id])
        params = task["params"]
        if task["type"] == "keyword":
            run_keyword_job(
                task_id, params["keyword"], params["lang"], params["state"], params["headless"], params.get("hydrate_fulltext", True)
            )
        elif task["type"] == "following":
            run_following_job(task_id, params["state"], params["headless"], params.get("hydrate_fulltext", True))
        elif task["type"] == "user_timeline":
            run_user_timeline_job(
                task_id, params["user_url"], params["state"], params["headless"], params.get("hydrate_fulltext", True)
            )
        elif task["type"] == "user_following":
            run_user_following_job(task_id, params["user_url"], params["state"], params["headless"])
        elif task["type"] == "zhihu_question":
            run_zhihu_question_job(
                task_id,
                params["question_url"],
                params["cookie"],
                params.get("user_agent", ""),
                params["headless"],
            )
        elif task["type"] == "zhihu_search":
            run_zhihu_search_job(
                task_id,
                params["keyword"],
                params["cookie"],
                params.get("user_agent", ""),
                params["headless"],
            )
        else:
            run_email_job(task_id, params)
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            cancelled = bool(task and task.get("cancel_requested"))
        if cancelled:
            finalized = finalize_partial_outputs(task_id, task["type"])
            message = "任务已停止，并已生成当前结果的汇总页面。" if finalized else "任务已停止。"
            update_task(task_id, status="cancelled", stage="已停止", message=message, progress=100)
        else:
            update_task(task_id, status="done", stage="已完成", progress=100)
    except Exception as exc:
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            cancelled = bool(task and task.get("cancel_requested"))
        if cancelled:
            task_type = task.get("type") if task else ""
            finalized = finalize_partial_outputs(task_id, task_type)
            message = "任务已停止，并已生成当前结果的汇总页面。" if finalized else "任务已停止。"
            update_task(task_id, status="cancelled", stage="已停止", message=message, progress=100)
            append_log(task_id, "[SYSTEM] 任务已按请求中止。")
        else:
            update_task(task_id, status="failed", stage="执行失败", error=str(exc), progress=100)
            append_log(task_id, f"[ERROR] {exc}")


def start_task(task_type: str, params: Dict) -> str:
    task_id = create_task(task_type, params)
    thread = threading.Thread(target=worker, args=(task_id,), daemon=True)
    thread.start()
    return task_id


def render_page() -> str:
    safe_cookie = html.escape(DEFAULT_ZHIHU_COOKIE)
    safe_user_agent = html.escape(DEFAULT_ZHIHU_USER_AGENT)
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>X Search Aggregator 控制台</title>
  <style>
    :root {
      --bg: #f2efe8;
      --ink: #1d1a16;
      --muted: #625d56;
      --line: #d7cdbd;
      --card: rgba(255,255,255,0.82);
      --accent: #0f766e;
      --accent-2: #c96d1d;
      --danger: #b74834;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "PingFang SC", "Noto Sans SC", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(900px 420px at 105% -10%, rgba(15,118,110,0.16), transparent 60%),
        radial-gradient(960px 440px at -5% 0%, rgba(201,109,29,0.14), transparent 58%),
        var(--bg);
    }
    .wrap { max-width: 1220px; margin: 0 auto; padding: 28px 18px 56px; }
    .hero {
      padding: 28px;
      background: linear-gradient(135deg, rgba(255,255,255,0.78), rgba(255,248,236,0.78));
      border: 1px solid rgba(255,255,255,0.7);
      border-radius: 28px;
      box-shadow: 0 24px 60px rgba(60,44,24,0.08);
      margin-bottom: 20px;
    }
    .hero h1 {
      margin: 0 0 8px;
      font-family: "Source Han Serif SC", "Noto Serif CJK SC", serif;
      font-size: clamp(2rem, 4vw, 3.4rem);
      letter-spacing: 0.01em;
    }
    .hero p { margin: 0; color: var(--muted); font-size: 1rem; }
    .layout {
      display: grid;
      grid-template-columns: minmax(340px, 420px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .stack { display: grid; gap: 16px; }
    .dual {
      display: grid;
      grid-template-columns: 1.05fr 1fr;
      gap: 16px;
    }
    .panel {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 20px;
      backdrop-filter: blur(10px);
    }
    .panel h2 { margin: 0 0 12px; font-size: 1.15rem; }
    .panel p { margin: 0 0 14px; color: var(--muted); line-height: 1.7; }
    .panel h3 { margin: 14px 0 8px; font-size: 0.98rem; }
    label { display: block; margin-bottom: 12px; font-size: 0.95rem; }
    input[type="text"], input[type="password"], select, textarea {
      width: 100%;
      margin-top: 6px;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid #d6ccbb;
      background: rgba(255,255,255,0.9);
      font-size: 0.96rem;
      color: var(--ink);
      font-family: inherit;
    }
    textarea {
      min-height: 108px;
      resize: vertical;
    }
    .checkbox { display: flex; align-items: center; gap: 10px; margin: 12px 0 16px; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font-size: 0.96rem;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      color: white;
      background: linear-gradient(90deg, var(--accent), #179987);
      box-shadow: 0 10px 24px rgba(15,118,110,0.22);
    }
    .btn.alt {
      background: linear-gradient(90deg, var(--accent-2), #ef9f51);
      box-shadow: 0 10px 24px rgba(201,109,29,0.2);
    }
    .btn:disabled { opacity: 0.55; cursor: wait; }
    .status-shell {
      min-height: 620px;
      background: linear-gradient(180deg, rgba(18,22,29,0.98), rgba(18,22,29,0.92));
      color: #e8eef8;
      border-radius: 26px;
      padding: 20px;
      box-shadow: 0 24px 60px rgba(19,21,26,0.16);
    }
    .task-list {
      display: grid;
      gap: 10px;
      margin-top: 12px;
      max-height: 360px;
      overflow: auto;
    }
    .task-card {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255,255,255,0.04);
      cursor: pointer;
    }
    .task-card.active {
      border-color: rgba(78,168,255,0.7);
      background: rgba(78,168,255,0.1);
    }
    .task-card-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .task-card-title { font-weight: 700; }
    .task-card-meta { color: #9fb1c8; font-size: 0.85rem; }
    .task-mini {
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      overflow: hidden;
      margin-top: 10px;
    }
    .task-mini > span {
      display: block;
      height: 100%;
      background: linear-gradient(90deg, #ffd166, #41c7b9 55%, #4ea8ff);
    }
    .status-top {
      display: flex;
      gap: 14px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      color: #b8c8dd;
      font-size: 0.88rem;
    }
    .progress {
      width: 100%;
      height: 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      overflow: hidden;
      margin-bottom: 12px;
    }
    .progress > span {
      display: block;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #ffd166, #41c7b9 55%, #4ea8ff);
      transition: width 0.35s ease;
    }
    .stage { font-size: 1.05rem; font-weight: 700; margin-bottom: 8px; }
    .subtle { color: #95a6bd; font-size: 0.9rem; }
    .facts {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 8px 0 10px;
    }
    .fact {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      color: #c5d3e4;
      font-size: 0.86rem;
      border: 1px solid rgba(255,255,255,0.08);
    }
    .message {
      margin: 14px 0 12px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(255,255,255,0.06);
      color: #dfe8f4;
    }
    .message.error {
      background: rgba(183,72,52,0.18);
      color: #ffd5cc;
      border: 1px solid rgba(183,72,52,0.35);
    }
    .result-links, .link-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .result-link, .link-chip {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 8px 12px;
      text-decoration: none;
      color: inherit;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
    }
    .result-link.light, .link-chip.light {
      color: var(--ink);
      border-color: #d8d0c2;
      background: rgba(255,255,255,0.9);
    }
    .run-list { display: grid; gap: 12px; }
    .run-card {
      padding: 16px;
      border: 1px solid #ddd3c6;
      border-radius: 18px;
      background: rgba(255,255,255,0.68);
    }
    .run-title { font-weight: 700; }
    .run-meta { margin-top: 4px; color: var(--muted); font-size: 0.9rem; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(3,7,13,0.8);
      color: #dce7f5;
      border-radius: 18px;
      padding: 16px;
      overflow: auto;
      max-height: 430px;
      min-height: 280px;
      margin: 12px 0 0;
      border: 1px solid rgba(255,255,255,0.08);
    }
    .muted { color: var(--muted); }
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .btn.ghost {
      background: rgba(255,255,255,0.08);
      box-shadow: none;
      border: 1px solid rgba(255,255,255,0.12);
      color: #e8eef8;
    }
    .btn.stop {
      background: linear-gradient(90deg, #b74834, #d6694c);
      box-shadow: 0 10px 24px rgba(183,72,52,0.22);
    }
    .service-box {
      border: 1px solid #ddd3c6;
      border-radius: 18px;
      padding: 16px;
      background: rgba(255,255,255,0.68);
    }
    .service-facts {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0 12px;
    }
    .service-chip {
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid #d8d0c2;
      background: rgba(255,255,255,0.9);
      font-size: 0.86rem;
      color: var(--ink);
    }
    .service-log {
      max-height: 220px;
      min-height: 160px;
      margin-top: 10px;
    }
    .mail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }
    .mail-grid.full {
      grid-template-columns: 1fr;
    }
    .selection-box {
      border: 1px solid #ddd3c6;
      border-radius: 18px;
      padding: 14px;
      background: rgba(255,255,255,0.72);
    }
    .selection-box strong {
      display: block;
      margin-bottom: 8px;
    }
    .option-list {
      display: grid;
      gap: 8px;
      max-height: 180px;
      overflow: auto;
      margin-top: 8px;
    }
    .option-item {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255,255,255,0.86);
      border: 1px solid #e0d8cb;
    }
    .option-item input {
      margin-top: 2px;
    }
    .option-item small {
      display: block;
      color: var(--muted);
      margin-top: 2px;
    }
    .mini-note {
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.6;
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      .dual { grid-template-columns: 1fr; }
      .status-shell { min-height: 0; }
      .mail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="wrap">
    <header class="hero">
      <h1>X 抓取控制台</h1>
      <p>提交任务后会在后台执行。右侧面板会自动刷新状态、进度和日志，完成后直接点开生成的 HTML。</p>
    </header>

    <section class="layout">
      <div class="stack">
        <form class="panel js-task-form" data-kind="keyword">
          <h2>关键词抓取并生成 HTML</h2>
          <p>执行关键词抓取、深度文章生成和价值评分排序。适合做专题情报页。</p>
          <label>关键词
            <input type="text" name="keyword" placeholder="例如 AI Agent / 信息差 / Crypto" required />
          </label>
          <label>语言过滤
            <input type="text" name="lang" placeholder="可留空，或填 zh / en" />
          </label>
          <label>登录态文件
            <input type="text" name="state" value="auth_state_cookie.json" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <label class="checkbox">
            <input type="checkbox" name="hydrate_fulltext" value="1" checked />
            <span>逐条进入详情页补全文</span>
          </label>
          <div class="mini-note">关闭后会只抓列表页摘要和链接，速度更快；开启后会补全长帖正文，并生成 `fulltext_progress.json`。</div>
          <button class="btn" type="submit">开始抓取关键词</button>
        </form>

        <form class="panel js-task-form" data-kind="following">
          <h2>抓取关注流并生成 HTML</h2>
          <p>抓取你关注账号的最新动态，再自动生成排序页、摘要页和文章页。</p>
          <label>登录态文件
            <input type="text" name="state" value="auth_state_cookie.json" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <label class="checkbox">
            <input type="checkbox" name="hydrate_fulltext" value="1" checked />
            <span>逐条进入详情页补全文</span>
          </label>
          <div class="mini-note">停止任务时，如果已经落盘了部分结果，系统会立即基于当前结果生成可查看的汇总页面。</div>
          <button class="btn alt" type="submit">开始抓取关注流</button>
        </form>

        <form class="panel js-task-form" data-kind="user_timeline">
          <h2>抓取某个博主全部历史推文</h2>
          <p>输入 `x.com` 用户主页，抓取该博主尽可能完整的历史推文，并生成文章页、详细报告和价值排序页。</p>
          <label>用户主页
            <input type="text" name="user_url" placeholder="https://x.com/elonmusk 或 @elonmusk" required />
          </label>
          <label>登录态文件
            <input type="text" name="state" value="auth_state_cookie.json" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <label class="checkbox">
            <input type="checkbox" name="hydrate_fulltext" value="1" checked />
            <span>逐条进入详情页补全文</span>
          </label>
          <div class="mini-note">运行中会周期性保存部分结果；如果中途点击停止，会尽量立即生成当前已抓取内容的汇总 HTML。</div>
          <button class="btn" type="submit">开始抓取历史推文</button>
        </form>

        <form class="panel js-task-form" data-kind="user_following">
          <h2>抓取某个博主关注用户列表</h2>
          <p>输入 `x.com` 用户主页，通过内部接口抓取其关注列表，并生成详细画像报告。</p>
          <label>用户主页
            <input type="text" name="user_url" placeholder="https://x.com/elonmusk 或 @elonmusk" required />
          </label>
          <label>登录态文件
            <input type="text" name="state" value="auth_state_cookie.json" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <button class="btn alt" type="submit">开始抓取关注列表</button>
        </form>

        <form class="panel js-task-form" data-kind="zhihu_question">
          <h2>抓取知乎问题的所有回答全文</h2>
          <p>输入 `zhihu.com/question/...` 问题链接，粘贴浏览器请求头里的 Cookie，系统会滚动问题页收集回答链接，再逐条进入回答页保存完整文本。</p>
          <label>问题链接
            <input type="text" name="question_url" placeholder="https://www.zhihu.com/question/547768388" required />
          </label>
          <label>Cookie 字符串
            <textarea name="cookie" placeholder="把浏览器 Network 里该请求的完整 Cookie 头粘贴到这里" required>__DEFAULT_ZHIHU_COOKIE__</textarea>
          </label>
          <label>User-Agent
            <input type="text" name="user_agent" value="__DEFAULT_ZHIHU_USER_AGENT__" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <div class="mini-note">当前实现依赖有效的知乎 Cookie。输出目录会包含 `results.json`、`results.csv`、`all_answers.md` 和 `article.html`。</div>
          <button class="btn" type="submit">开始抓取知乎回答</button>
        </form>

        <form class="panel js-task-form" data-kind="zhihu_search">
          <h2>抓取知乎搜索前 500 条结果全文</h2>
          <p>输入关键词后，系统会先在知乎搜索页抓取前 500 条内容结果的摘要和链接保存到 `results_stage1.json`，再逐条打开链接补全正文保存到 `results.json`。</p>
          <label>搜索关键词
            <input type="text" name="keyword" placeholder="例如 自动驾驶强化学习 / AI Agent" required />
          </label>
          <label>Cookie 字符串
            <textarea name="cookie" placeholder="把浏览器 Network 里知乎请求的完整 Cookie 头粘贴到这里" required>__DEFAULT_ZHIHU_COOKIE__</textarea>
          </label>
          <label>User-Agent
            <input type="text" name="user_agent" value="__DEFAULT_ZHIHU_USER_AGENT__" />
          </label>
          <label class="checkbox">
            <input type="checkbox" name="headless" value="1" checked />
            <span>无头模式运行</span>
          </label>
          <div class="mini-note">输出目录会包含 `results_stage1.json`、`results.json`、`results.csv`、`all_results.md`、`fulltext_progress.json` 和 `article.html`。</div>
          <button class="btn alt" type="submit">开始抓取知乎搜索</button>
        </form>

        <section class="panel">
          <h2>一键批量发邮件</h2>
          <p>把已生成的报告作为附件，按收件人列表逐封发送。适合把结果直接推给客户、团队或订阅用户。</p>
          <div class="mail-grid">
            <label>SMTP Host
              <input type="text" id="smtpHost" placeholder="smtp.qq.com / smtp.gmail.com" />
            </label>
            <label>SMTP Port
              <input type="text" id="smtpPort" placeholder="587" />
            </label>
            <label>加密方式
              <select id="smtpSecurity">
                <option value="starttls">STARTTLS</option>
                <option value="ssl">SSL</option>
                <option value="none">无加密</option>
              </select>
            </label>
            <label>SMTP 用户名
              <input type="text" id="smtpUsername" placeholder="通常是邮箱地址" />
            </label>
            <label>SMTP 密码 / 授权码
              <input type="password" id="smtpPassword" placeholder="留空则保留已保存密码" />
            </label>
            <label>发件邮箱
              <input type="text" id="senderEmail" placeholder="noreply@example.com" />
            </label>
          </div>
          <div class="mail-grid" style="margin-top: 8px;">
            <label>发件人名称
              <input type="text" id="senderName" placeholder="X Search Aggregator" />
            </label>
            <label>发送标题
              <input type="text" id="emailSubject" placeholder="本周 X 情报简报" />
            </label>
          </div>
          <label>收件人列表
            <textarea id="emailRecipients" placeholder="支持逗号、空格或换行分隔，例如&#10;a@example.com&#10;b@example.com"></textarea>
          </label>
          <label>正文
            <textarea id="emailBody" placeholder="这里填写邮件正文。系统会自动附带输出目录和附件列表说明。"></textarea>
          </label>
          <div class="mail-grid full">
            <label>选择输出目录
              <select id="runSelect"></select>
            </label>
          </div>
          <div class="selection-box">
            <strong>要发送的附件（可选）</strong>
            <div class="mini-note" id="attachmentHint">先选择一个输出目录。</div>
            <div class="option-list" id="attachmentList"></div>
          </div>
          <div class="toolbar">
            <button class="btn ghost" id="saveMailerBtn" type="button">保存 SMTP 配置</button>
            <button class="btn alt" id="sendEmailBtn" type="button">一键批量发送</button>
          </div>
          <div id="mailerMessage"></div>
            <div class="mini-note">SMTP 配置会保存在本地 `output/.web_mailer.json`。密码输入留空时，不会覆盖已保存值。没有可选附件时，也可以直接发纯正文邮件。</div>
        </section>

        <section class="panel">
          <h2>BettaFish 集成</h2>
          <p>接入官方 BettaFish 多 Agent 舆情系统。这里负责管理它的本地路径、启动状态和访问入口，不重写它本身的分析逻辑。</p>
          <label>BettaFish 本地路径
            <input type="text" id="bettafishPath" placeholder="/path/to/BettaFish" />
          </label>
          <label>Python 命令
            <input type="text" id="bettafishPython" placeholder="python" />
          </label>
          <label>Host
            <input type="text" id="bettafishHost" placeholder="127.0.0.1" />
          </label>
          <label>Port
            <input type="text" id="bettafishPort" placeholder="5000" />
          </label>
          <div class="toolbar">
            <button class="btn ghost" id="saveBettafishBtn" type="button">保存配置</button>
            <button class="btn" id="startBettafishBtn" type="button">启动 BettaFish</button>
            <button class="btn stop" id="stopBettafishBtn" type="button">停止 BettaFish</button>
            <a class="btn alt" id="openBettafishBtn" href="#" target="_blank" rel="noopener">打开 BettaFish</a>
          </div>
          <div class="service-box" style="margin-top: 14px;">
            <div class="task-card-top">
              <div class="task-card-title">服务状态</div>
              <div class="badge" id="bettafishBadge">idle</div>
            </div>
            <div class="task-card-meta" id="bettafishMeta">尚未配置 BettaFish 路径。</div>
            <div class="service-facts" id="bettafishFacts"></div>
            <div id="bettafishMessage"></div>
            <pre class="service-log" id="bettafishLogs">暂无 BettaFish 日志。</pre>
          </div>
        </section>

        <section class="panel">
          <h2>最近生成的结果</h2>
          <div class="run-list" id="recentRuns"></div>
        </section>
      </div>

      <section class="status-shell">
        <div class="dual">
          <section>
            <div class="status-top">
              <div>
                <div class="stage" id="taskStage">当前没有运行中的任务</div>
                <div class="subtle" id="taskMeta">提交任务后会在这里显示详细进度。</div>
              </div>
              <div class="badge" id="taskBadge">空闲</div>
            </div>

            <div class="progress"><span id="progressBar"></span></div>
            <div class="subtle" id="progressText">0%</div>
            <div id="taskFacts" class="facts"></div>
            <div class="toolbar">
              <button class="btn ghost" id="refreshTasksBtn" type="button">刷新任务列表</button>
              <button class="btn stop" id="stopTaskBtn" type="button" disabled>停止当前任务</button>
            </div>
            <div id="taskMessage"></div>
            <div id="taskLinks" class="result-links"></div>

            <pre id="taskLogs">暂无日志。</pre>
          </section>

          <section>
            <div class="status-top">
              <div>
                <div class="stage">任务队列</div>
                <div class="subtle">点击任意任务可查看详情；运行中的任务会自动刷新。</div>
              </div>
            </div>
            <div id="taskList" class="task-list"></div>
          </section>
        </div>
      </section>
    </section>
  </main>

  <script>
    const recentRunsEl = document.getElementById("recentRuns");
    const taskStageEl = document.getElementById("taskStage");
    const taskMetaEl = document.getElementById("taskMeta");
    const taskBadgeEl = document.getElementById("taskBadge");
    const progressBarEl = document.getElementById("progressBar");
    const progressTextEl = document.getElementById("progressText");
    const taskFactsEl = document.getElementById("taskFacts");
    const taskMessageEl = document.getElementById("taskMessage");
    const taskLinksEl = document.getElementById("taskLinks");
    const taskLogsEl = document.getElementById("taskLogs");
    const taskListEl = document.getElementById("taskList");
    const refreshTasksBtn = document.getElementById("refreshTasksBtn");
    const stopTaskBtn = document.getElementById("stopTaskBtn");
    const bettafishPathEl = document.getElementById("bettafishPath");
    const bettafishPythonEl = document.getElementById("bettafishPython");
    const bettafishHostEl = document.getElementById("bettafishHost");
    const bettafishPortEl = document.getElementById("bettafishPort");
    const saveBettafishBtn = document.getElementById("saveBettafishBtn");
    const startBettafishBtn = document.getElementById("startBettafishBtn");
    const stopBettafishBtn = document.getElementById("stopBettafishBtn");
    const openBettafishBtn = document.getElementById("openBettafishBtn");
    const bettafishBadgeEl = document.getElementById("bettafishBadge");
    const bettafishMetaEl = document.getElementById("bettafishMeta");
    const bettafishFactsEl = document.getElementById("bettafishFacts");
    const bettafishMessageEl = document.getElementById("bettafishMessage");
    const bettafishLogsEl = document.getElementById("bettafishLogs");
    const smtpHostEl = document.getElementById("smtpHost");
    const smtpPortEl = document.getElementById("smtpPort");
    const smtpSecurityEl = document.getElementById("smtpSecurity");
    const smtpUsernameEl = document.getElementById("smtpUsername");
    const smtpPasswordEl = document.getElementById("smtpPassword");
    const senderEmailEl = document.getElementById("senderEmail");
    const senderNameEl = document.getElementById("senderName");
    const emailSubjectEl = document.getElementById("emailSubject");
    const emailRecipientsEl = document.getElementById("emailRecipients");
    const emailBodyEl = document.getElementById("emailBody");
    const runSelectEl = document.getElementById("runSelect");
    const attachmentListEl = document.getElementById("attachmentList");
    const attachmentHintEl = document.getElementById("attachmentHint");
    const mailerMessageEl = document.getElementById("mailerMessage");
    const saveMailerBtn = document.getElementById("saveMailerBtn");
    const sendEmailBtn = document.getElementById("sendEmailBtn");

    let currentTaskId = null;
    let timerId = null;
    let integrationTimerId = null;
    let recentRuns = [];

    function buildFacts(task) {
      const facts = [];
      if (task.collected_items || task.target_items) {
        const target = task.target_items ? ` / ${task.target_items}` : "";
        facts.push(`已采集 ${task.collected_items || 0}${target} 条`);
      }
      if (task.current_scroll || task.max_scrolls) {
        const total = task.max_scrolls ? ` / ${task.max_scrolls}` : "";
        facts.push(`滚动 ${task.current_scroll || 0}${total}`);
      }
      if (task.last_new_items) {
        facts.push(`最近新增 ${task.last_new_items} 条`);
      }
      if (task.fulltext_total) {
        facts.push(`全文 ${task.fulltext_processed || 0} / ${task.fulltext_total}`);
      }
      if (task.fulltext_hydrated) {
        facts.push(`补全成功 ${task.fulltext_hydrated}`);
      }
      if (task.fulltext_failed) {
        facts.push(`补全失败 ${task.fulltext_failed}`);
      }
      return facts;
    }

    function escapeHtml(text) {
      return String(text || "").replace(/[&<>"]/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;"
      }[ch]));
    }

    function renderBettafish(data) {
      bettafishPathEl.value = data.path || "";
      bettafishPythonEl.value = data.python_cmd || "python";
      bettafishHostEl.value = data.host || "127.0.0.1";
      bettafishPortEl.value = data.port || 5000;
      bettafishBadgeEl.textContent = data.status || "idle";
      bettafishMetaEl.textContent = `地址 ${data.url || "-"} · 更新于 ${data.updated_at || "-"}`;
      openBettafishBtn.href = data.url || "#";
      openBettafishBtn.style.pointerEvents = data.url ? "auto" : "none";
      openBettafishBtn.style.opacity = data.url ? "1" : "0.5";
      const facts = [
        `在线检测: ${data.online ? "online" : "offline"}`,
        `PID: ${data.pid || "-"}`,
        `Host: ${data.host || "-"}`,
        `Port: ${data.port || "-"}`,
      ];
      bettafishFactsEl.innerHTML = facts.map((fact) => `<span class="service-chip">${escapeHtml(fact)}</span>`).join("");
      let messageHtml = "";
      if (data.error) {
        messageHtml = `<div class="message error">${escapeHtml(data.error)}</div>`;
      } else if (data.message) {
        messageHtml = `<div class="message">${escapeHtml(data.message)}</div>`;
      }
      bettafishMessageEl.innerHTML = messageHtml;
      bettafishLogsEl.textContent = data.logs || "暂无 BettaFish 日志。";
      bettafishLogsEl.scrollTop = bettafishLogsEl.scrollHeight;
      startBettafishBtn.disabled = ["starting", "running"].includes(data.status);
      stopBettafishBtn.disabled = !["starting", "running", "online", "stopping"].includes(data.status);
    }

    async function refreshBettafish() {
      const res = await fetch("/api/integrations/bettafish");
      const data = await res.json();
      renderBettafish(data);
    }

    function selectedAttachments() {
      return Array.from(document.querySelectorAll(".js-attachment:checked")).map((item) => item.value);
    }

    function renderAttachmentOptions() {
      const selectedRun = recentRuns.find((item) => item.name === runSelectEl.value);
      if (!selectedRun) {
        attachmentHintEl.textContent = "先选择一个输出目录。";
        attachmentListEl.innerHTML = "";
        return;
      }
      const files = selectedRun.files || [];
      if (!files.length) {
        attachmentHintEl.textContent = "这个目录下没有可发送的标准结果文件。";
        attachmentListEl.innerHTML = "";
        return;
      }
      attachmentHintEl.textContent = `当前目录 ${selectedRun.name}，默认全选 ${files.length} 个附件。`;
      attachmentListEl.innerHTML = files.map((file) => `
        <label class="option-item">
          <input class="js-attachment" type="checkbox" value="${escapeHtml(file.name)}" checked />
          <span>
            <strong>${escapeHtml(file.label)}</strong>
            <small>${escapeHtml(file.name)}</small>
          </span>
        </label>
      `).join("");
    }

    function renderMailerConfig(data) {
      smtpHostEl.value = data.smtp_host || "";
      smtpPortEl.value = data.smtp_port || 587;
      smtpSecurityEl.value = data.smtp_security || "starttls";
      smtpUsernameEl.value = data.username || "";
      senderEmailEl.value = data.sender_email || "";
      senderNameEl.value = data.sender_name || "";
      smtpPasswordEl.value = "";
      let html = "";
      if (data.updated_at) {
        const passwordStatus = data.has_password ? "已保存密码" : "未保存密码";
        html = `<div class="message">SMTP 配置已加载。${escapeHtml(passwordStatus)}，更新时间 ${escapeHtml(data.updated_at)}。</div>`;
      }
      mailerMessageEl.innerHTML = html;
    }

    async function refreshMailerConfig() {
      const res = await fetch("/api/mailer/config");
      const data = await res.json();
      renderMailerConfig(data);
    }

    function renderRecentRuns(items) {
      recentRuns = items;
      if (!items.length) {
        recentRunsEl.innerHTML = '<span class="muted">当前还没有输出目录。</span>';
        runSelectEl.innerHTML = '<option value="">暂无输出目录</option>';
        renderAttachmentOptions();
        return;
      }
      const current = runSelectEl.value;
      runSelectEl.innerHTML = items.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${escapeHtml(item.updated_at)}</option>`).join("");
      const hasCurrent = items.some((item) => item.name === current);
      runSelectEl.value = hasCurrent ? current : items[0].name;
      renderAttachmentOptions();
      recentRunsEl.innerHTML = items.map((item) => `
        <article class="run-card">
          <div class="run-title">${escapeHtml(item.name)}</div>
          <div class="run-meta">更新时间 ${escapeHtml(item.updated_at)}</div>
          <div class="link-row">
            ${item.links.length ? item.links.map((link) => `<a class="link-chip light" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.label)}</a>`).join("") : '<span class="muted">暂无可打开文件</span>'}
          </div>
        </article>
      `).join("");
    }

    async function refreshRecentRuns() {
      const res = await fetch("/api/runs");
      const data = await res.json();
      renderRecentRuns(data.runs || []);
    }

    function setIdleView() {
      taskStageEl.textContent = "当前没有运行中的任务";
      taskMetaEl.textContent = "提交任务后会在这里显示详细进度。";
      taskBadgeEl.textContent = "空闲";
      progressBarEl.style.width = "0%";
      progressTextEl.textContent = "0%";
      taskFactsEl.innerHTML = "";
      taskMessageEl.innerHTML = "";
      taskLinksEl.innerHTML = "";
      taskLogsEl.textContent = "暂无日志。";
      stopTaskBtn.disabled = true;
    }

    function renderTaskList(tasks) {
      if (!tasks.length) {
        taskListEl.innerHTML = '<span class="subtle">还没有任务记录。</span>';
        return;
      }
      taskListEl.innerHTML = tasks.map((task) => `
        <article class="task-card ${task.id === currentTaskId ? "active" : ""}" data-task-id="${escapeHtml(task.id)}">
          <div class="task-card-top">
            <div class="task-card-title">${escapeHtml(task.type)} · ${escapeHtml(task.id)}</div>
            <div class="badge">${escapeHtml(task.status)}</div>
          </div>
          <div class="task-card-meta">${escapeHtml(task.stage)} · ${escapeHtml(task.created_at)}</div>
          <div class="task-card-meta">${buildFacts(task).join(" · ") || "暂无实时抓取指标"}</div>
          <div class="task-card-meta">${task.result_dir ? `输出：${escapeHtml(task.result_dir)}` : (task.error ? `错误：${escapeHtml(task.error)}` : escapeHtml(task.message || ""))}</div>
          <div class="task-mini"><span style="width:${task.progress || 0}%"></span></div>
        </article>
      `).join("");
      taskListEl.querySelectorAll(".task-card").forEach((card) => {
        card.addEventListener("click", async () => {
          currentTaskId = card.dataset.taskId;
          await refreshTaskList();
          await pollTask(currentTaskId, false);
        });
      });
    }

    async function refreshTaskList() {
      const res = await fetch("/api/tasks");
      const data = await res.json();
      renderTaskList(data.tasks || []);
    }

    function renderTask(task) {
      taskStageEl.textContent = task.stage || "处理中";
      taskMetaEl.textContent = `任务 ${task.id} · ${task.type} · 创建于 ${task.created_at} · 更新于 ${task.updated_at}`;
      taskBadgeEl.textContent = task.status;
      progressBarEl.style.width = `${task.progress || 0}%`;
      progressTextEl.textContent = `${task.progress || 0}%`;
      taskFactsEl.innerHTML = buildFacts(task).map((fact) => `<span class="fact">${escapeHtml(fact)}</span>`).join("");

      let messageHtml = "";
      if (task.error) {
        messageHtml = `<div class="message error">${escapeHtml(task.error)}</div>`;
      } else if (task.message) {
        messageHtml = `<div class="message">${escapeHtml(task.message)}</div>`;
      } else if (task.result_dir) {
        messageHtml = `<div class="message">输出目录：${escapeHtml(task.result_dir)}</div>`;
      }
      taskMessageEl.innerHTML = messageHtml;

      taskLinksEl.innerHTML = (task.result_links || []).map((link) =>
        `<a class="result-link" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.label)}</a>`
      ).join("");

      taskLogsEl.textContent = task.logs || "暂无日志。";
      taskLogsEl.scrollTop = taskLogsEl.scrollHeight;
      stopTaskBtn.disabled = !["queued", "running", "cancelling"].includes(task.status);
    }

    async function pollTask(taskId, refreshList = true) {
      const res = await fetch(`/api/tasks/${taskId}`);
      const data = await res.json();
      if (data.error) {
        taskMessageEl.innerHTML = `<div class="message error">${escapeHtml(data.error)}</div>`;
        return;
      }
      renderTask(data);
      if (refreshList) {
        await refreshTaskList();
      }
      if (["done", "failed", "cancelled"].includes(data.status)) {
        clearInterval(timerId);
        timerId = null;
        await refreshRecentRuns();
        await refreshTaskList();
      }
    }

    async function submitTask(form) {
      const formData = new FormData(form);
      const kind = form.dataset.kind;
      const endpointMap = {
        keyword: "/api/tasks/keyword",
        following: "/api/tasks/following",
        user_timeline: "/api/tasks/user-timeline",
        user_following: "/api/tasks/user-following",
        zhihu_question: "/api/tasks/zhihu-question",
        zhihu_search: "/api/tasks/zhihu-search"
      };
      const endpoint = endpointMap[kind];
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      try {
        const res = await fetch(endpoint, { method: "POST", body: formData });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "任务创建失败");
        }
        currentTaskId = data.task_id;
        if (timerId) {
          clearInterval(timerId);
        }
        await pollTask(currentTaskId);
        timerId = setInterval(() => pollTask(currentTaskId), 2000);
      } catch (err) {
        taskMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      } finally {
        button.disabled = false;
      }
    }

    async function stopCurrentTask() {
      if (!currentTaskId) {
        return;
      }
      stopTaskBtn.disabled = true;
      try {
        const res = await fetch(`/api/tasks/${currentTaskId}/stop`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "停止任务失败");
        }
        await pollTask(currentTaskId);
      } catch (err) {
        taskMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      }
    }

    async function saveMailerConfig() {
      const payload = {
        smtp_host: smtpHostEl.value.trim(),
        smtp_port: smtpPortEl.value.trim(),
        smtp_security: smtpSecurityEl.value,
        username: smtpUsernameEl.value.trim(),
        password: smtpPasswordEl.value,
        sender_email: senderEmailEl.value.trim(),
        sender_name: senderNameEl.value.trim(),
      };
      const res = await fetch("/api/mailer/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "保存 SMTP 配置失败");
      }
      renderMailerConfig(data);
    }

    async function submitEmailTask() {
      sendEmailBtn.disabled = true;
      try {
        await saveMailerConfig();
        const payload = {
          recipients: emailRecipientsEl.value,
          subject: emailSubjectEl.value.trim(),
          body: emailBodyEl.value,
          run_name: runSelectEl.value,
          attachments: selectedAttachments(),
        };
        const res = await fetch("/api/tasks/email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "创建邮件任务失败");
        }
        currentTaskId = data.task_id;
        if (timerId) {
          clearInterval(timerId);
        }
        mailerMessageEl.innerHTML = `<div class="message">邮件任务已提交，正在批量发送。</div>`;
        await pollTask(currentTaskId);
        timerId = setInterval(() => pollTask(currentTaskId), 2000);
      } catch (err) {
        mailerMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      } finally {
        sendEmailBtn.disabled = false;
      }
    }

    async function saveBettafishConfig() {
      const payload = {
        path: bettafishPathEl.value.trim(),
        python_cmd: bettafishPythonEl.value.trim(),
        host: bettafishHostEl.value.trim(),
        port: bettafishPortEl.value.trim(),
      };
      const res = await fetch("/api/integrations/bettafish/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "保存 BettaFish 配置失败");
      }
      renderBettafish(data);
    }

    async function startBettafish() {
      try {
        await saveBettafishConfig();
        const res = await fetch("/api/integrations/bettafish/start", { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "启动 BettaFish 失败");
        }
        renderBettafish(data);
      } catch (err) {
        bettafishMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      }
    }

    async function stopBettafish() {
      try {
        const res = await fetch("/api/integrations/bettafish/stop", { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "停止 BettaFish 失败");
        }
        renderBettafish(data);
      } catch (err) {
        bettafishMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      }
    }

    document.querySelectorAll(".js-task-form").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await submitTask(form);
      });
    });
    refreshTasksBtn.addEventListener("click", refreshTaskList);
    stopTaskBtn.addEventListener("click", stopCurrentTask);
    runSelectEl.addEventListener("change", renderAttachmentOptions);
    saveMailerBtn.addEventListener("click", async () => {
      try {
        await saveMailerConfig();
      } catch (err) {
        mailerMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      }
    });
    sendEmailBtn.addEventListener("click", submitEmailTask);
    saveBettafishBtn.addEventListener("click", async () => {
      try {
        await saveBettafishConfig();
      } catch (err) {
        bettafishMessageEl.innerHTML = `<div class="message error">${escapeHtml(err.message || err)}</div>`;
      }
    });
    startBettafishBtn.addEventListener("click", startBettafish);
    stopBettafishBtn.addEventListener("click", stopBettafish);

    refreshRecentRuns();
    refreshTaskList();
    refreshMailerConfig();
    refreshBettafish();
    integrationTimerId = setInterval(refreshBettafish, 3000);
    setIdleView();
  </script>
</body>
</html>""".replace("__DEFAULT_ZHIHU_COOKIE__", safe_cookie).replace(
        "__DEFAULT_ZHIHU_USER_AGENT__", safe_user_agent
    )


@app.get("/")
def index() -> str:
    return render_page()


@app.get("/api/runs")
def api_runs():
    return jsonify({"runs": recent_runs_payload()})


@app.get("/api/mailer/config")
def api_mailer_config():
    return jsonify(mailer_payload())


@app.post("/api/mailer/config")
def api_mailer_config_save():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(update_mailer(payload))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/integrations/bettafish")
def api_bettafish():
    return jsonify(integration_payload("bettafish"))


@app.post("/api/integrations/bettafish/config")
def api_bettafish_config():
    payload = request.get_json(silent=True) or {}
    path = str(payload.get("path", "")).strip()
    python_cmd = str(payload.get("python_cmd", "python")).strip() or "python"
    host = str(payload.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    raw_port = str(payload.get("port", "5000")).strip() or "5000"
    try:
        port = int(raw_port)
    except ValueError:
        return jsonify({"error": "BettaFish 端口必须是整数。"}), 400
    update_integration(
        "bettafish",
        path=path,
        python_cmd=python_cmd,
        host=host,
        port=port,
        message="BettaFish 配置已保存。",
        error="",
    )
    return jsonify(integration_payload("bettafish"))


@app.post("/api/integrations/bettafish/start")
def api_bettafish_start():
    try:
        return jsonify(start_bettafish_service())
    except Exception as exc:
        update_integration("bettafish", status="error", error=str(exc), message="")
        return jsonify({"error": str(exc)}), 400


@app.post("/api/integrations/bettafish/stop")
def api_bettafish_stop():
    return jsonify(stop_bettafish_service())


@app.get("/api/tasks")
def api_tasks():
    return jsonify({"tasks": list_tasks_payload()})


@app.post("/api/tasks/keyword")
def api_task_keyword():
    keyword = (request.form.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "关键词不能为空。"}), 400
    task_id = start_task(
        "keyword",
        {
            "keyword": keyword,
            "lang": (request.form.get("lang") or "").strip(),
            "state": (request.form.get("state") or DEFAULT_STATE).strip(),
            "headless": request.form.get("headless") == "1",
            "hydrate_fulltext": request.form.get("hydrate_fulltext") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/following")
def api_task_following():
    task_id = start_task(
        "following",
        {
            "state": (request.form.get("state") or DEFAULT_STATE).strip(),
            "headless": request.form.get("headless") == "1",
            "hydrate_fulltext": request.form.get("hydrate_fulltext") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/user-timeline")
def api_task_user_timeline():
    user_url = (request.form.get("user_url") or "").strip()
    if not user_url:
        return jsonify({"error": "用户主页不能为空。"}), 400
    task_id = start_task(
        "user_timeline",
        {
            "user_url": user_url,
            "state": (request.form.get("state") or DEFAULT_STATE).strip(),
            "headless": request.form.get("headless") == "1",
            "hydrate_fulltext": request.form.get("hydrate_fulltext") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/user-following")
def api_task_user_following():
    user_url = (request.form.get("user_url") or "").strip()
    if not user_url:
        return jsonify({"error": "用户主页不能为空。"}), 400
    task_id = start_task(
        "user_following",
        {
            "user_url": user_url,
            "state": (request.form.get("state") or DEFAULT_STATE).strip(),
            "headless": request.form.get("headless") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/zhihu-question")
def api_task_zhihu_question():
    question_url = (request.form.get("question_url") or "").strip()
    cookie = (request.form.get("cookie") or "").strip()
    if not question_url:
        return jsonify({"error": "知乎问题链接不能为空。"}), 400
    if not cookie:
        return jsonify({"error": "Cookie 不能为空。"}), 400
    task_id = start_task(
        "zhihu_question",
        {
            "question_url": question_url,
            "cookie": cookie,
            "user_agent": (request.form.get("user_agent") or "").strip(),
            "headless": request.form.get("headless") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/zhihu-search")
def api_task_zhihu_search():
    keyword = (request.form.get("keyword") or "").strip()
    cookie = (request.form.get("cookie") or "").strip()
    if not keyword:
        return jsonify({"error": "知乎搜索关键词不能为空。"}), 400
    if not cookie:
        return jsonify({"error": "Cookie 不能为空。"}), 400
    task_id = start_task(
        "zhihu_search",
        {
            "keyword": keyword,
            "cookie": cookie,
            "user_agent": (request.form.get("user_agent") or "").strip(),
            "headless": request.form.get("headless") == "1",
        },
    )
    return jsonify({"task_id": task_id})


@app.post("/api/tasks/email")
def api_task_email():
    payload = request.get_json(silent=True) or {}
    task_id = start_task(
        "email",
        {
            "recipients": str(payload.get("recipients", "")),
            "subject": str(payload.get("subject", "")).strip(),
            "body": str(payload.get("body", "")),
            "run_name": str(payload.get("run_name", "")).strip(),
            "attachments": list(payload.get("attachments", [])),
        },
    )
    return jsonify({"task_id": task_id})


@app.get("/api/tasks/<task_id>")
def api_task_status(task_id: str):
    payload = task_payload(task_id)
    if payload.get("error") == "task not found":
        return jsonify(payload), 404
    return jsonify(payload)


@app.post("/api/tasks/<task_id>/stop")
def api_task_stop(task_id: str):
    payload = task_payload(task_id)
    if payload.get("error") == "task not found":
        return jsonify(payload), 404
    if payload["status"] in {"done", "failed", "cancelled"}:
        return jsonify({"error": "任务已经结束，无需停止。"}), 400
    terminate_task_process(task_id)
    return jsonify(task_payload(task_id))


@app.get("/files/<path:relpath>")
def serve_file(relpath: str):
    target = (BASE_DIR / relpath).resolve()
    if BASE_DIR not in target.parents and target != BASE_DIR:
        return Response("invalid path", status=400)
    return send_from_directory(target.parent, target.name)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    load_tasks_from_disk()
    load_integrations_from_disk()
    load_mailer_from_disk()
    host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("PORT", "8080").strip() or "8080")
    app.run(host=host, port=port, debug=False, threaded=True)

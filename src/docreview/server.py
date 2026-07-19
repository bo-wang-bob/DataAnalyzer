import base64
import hashlib
import hmac as hmac_module
import json
import os
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlencode, urlparse

from .db import ReviewDatabase
from .exporter import export_review_workbook
from .matching import parse_keyword_text
from .models import AppSettings
from .ocr import create_ocr_engine
from .service import Analyzer
from .web import PAGE_SIZE, h, highlight_text, layout, shorten, status_class


@dataclass(frozen=True)
class ServerLimits:
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    max_files: int = 2_000


class ServerJobManager:
    """Persistent, single-worker job queue suitable for one shared GPU."""

    def __init__(self, settings: AppSettings, storage_root: Path):
        self.settings = settings
        self.storage_root = storage_root.expanduser().resolve()
        self.jobs_root = self.storage_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docreview-gpu")
        self.jobs: dict[str, dict] = {}
        self.export_locks: dict[str, threading.Lock] = {}
        self.ocr_engine = None
        self.delete_uploads_after_analysis = _env_enabled(
            "DOCREVIEW_DELETE_UPLOADS_AFTER_ANALYSIS"
        )
        self._load_jobs()

    def _load_jobs(self) -> None:
        recover: list[str] = []
        cleanup: list[str] = []
        for metadata_path in sorted(self.jobs_root.glob("*/job.json")):
            try:
                record = json.loads(metadata_path.read_text(encoding="utf-8"))
                job_id = str(record["id"])
                if not _valid_job_id(job_id) or metadata_path.parent.name != job_id:
                    raise ValueError("任务编号无效")
                self.jobs[job_id] = record
                status = record.get("status")
                if status in {"queued", "running"}:
                    record["status"] = "queued"
                    record["message"] = "服务重启，任务已重新进入队列"
                    self._write_record(record)
                    recover.append(job_id)
                elif status == "uploading":
                    record["status"] = "error"
                    record["error"] = "服务在上传过程中重启，请重新提交任务"
                    self._write_record(record)
                    cleanup.append(job_id)
                elif status in {"complete", "error"}:
                    cleanup.append(job_id)
            except Exception as exc:
                print(f"[server] 无法加载任务 {metadata_path}: {exc}")
        for job_id in recover:
            self.submit(job_id)
        for job_id in cleanup:
            self._delete_analysis_sources(job_id)

    def create_record(self, name: str, keywords: str) -> dict:
        job_id = secrets.token_hex(10)
        now = _utc_now()
        record = {
            "id": job_id,
            "name": name.strip()[:100] or f"分析任务 {now[:19]}",
            "keywords": keywords,
            "status": "uploading",
            "message": "正在接收上传文件",
            "error": "",
            "current": 0,
            "total": 0,
            "file_count": 0,
            "upload_bytes": 0,
            "source_files_deleted": False,
            "source_files_deleted_at": "",
            "source_cleanup_error": "",
            "created_at": now,
            "updated_at": now,
        }
        job_dir = self.job_dir(job_id)
        (job_dir / "uploads").mkdir(parents=True, exist_ok=False)
        with self.lock:
            self.jobs[job_id] = record
            self._write_record(record)
        return dict(record)

    def finish_upload(self, job_id: str, file_count: int, upload_bytes: int) -> None:
        self.update(
            job_id,
            status="queued",
            message="文件上传完成，等待服务器分析",
            file_count=file_count,
            upload_bytes=upload_bytes,
        )
        self.database(job_id)
        self.submit(job_id)

    def fail_upload(self, job_id: str, message: str) -> None:
        self.update(job_id, status="error", message="上传失败", error=message)

    def submit(self, job_id: str) -> None:
        self.executor.submit(self._run_job, job_id)

    def _run_job(self, job_id: str) -> None:
        record = self.get(job_id)
        if not record:
            return
        self.update(job_id, status="running", message="正在开始分析", error="")
        try:
            rules = parse_keyword_text(str(record.get("keywords", "")))
            if not rules:
                raise ValueError("至少输入一个关键词")
            job_settings = self.job_settings(job_id)
            if self.ocr_engine is None:
                self.ocr_engine = create_ocr_engine(
                    job_settings.ocr_backend,
                    job_settings.root_dir,
                    job_settings.paddle_device,
                    job_settings.paddle_lang,
                )
            analyzer = Analyzer(
                job_settings,
                self.database(job_id),
                ocr_engine=self.ocr_engine,
            )
            analyzer.analyze_directory(
                self.upload_dir(job_id),
                rules,
                progress=lambda current, total, message: self.update(
                    job_id,
                    current=current,
                    total=total,
                    message=message,
                ),
            )
            self.update(job_id, status="complete", message="分析完成")
        except Exception as exc:
            self.update(job_id, status="error", message="分析失败", error=str(exc))
        finally:
            self._delete_analysis_sources(job_id)

    def _delete_analysis_sources(self, job_id: str) -> None:
        """Delete uploaded and reproducible working files, preserving review evidence."""
        if not self.delete_uploads_after_analysis:
            return
        job_dir = self.job_dir(job_id)
        targets = (
            job_dir / "uploads",
            job_dir / "data" / "pages",
            job_dir / "data" / "regions",
            job_dir / "data" / "converted",
        )
        errors: list[str] = []
        for target in targets:
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink(missing_ok=True)
                elif target.exists():
                    shutil.rmtree(target)
            except OSError as exc:
                errors.append(f"{target.name}: {exc}")
        remaining = [target.name for target in targets if target.exists()]
        if remaining:
            errors.append("仍存在: " + ", ".join(remaining))
        if errors:
            self.update(
                job_id,
                source_files_deleted=False,
                source_cleanup_error="; ".join(errors),
            )
            return
        self.update(
            job_id,
            source_files_deleted=True,
            source_files_deleted_at=_utc_now(),
            source_cleanup_error="",
        )

    def update(self, job_id: str, **values) -> None:
        with self.lock:
            record = self.jobs.get(job_id)
            if not record:
                return
            record.update(values)
            record["updated_at"] = _utc_now()
            self._write_record(record)

    def get(self, job_id: str) -> dict | None:
        if not _valid_job_id(job_id):
            return None
        with self.lock:
            record = self.jobs.get(job_id)
            return dict(record) if record else None

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            records = [dict(record) for record in self.jobs.values()]
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)[:limit]

    def job_dir(self, job_id: str) -> Path:
        if not _valid_job_id(job_id):
            raise ValueError("无效任务编号")
        return self.jobs_root / job_id

    def upload_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "uploads"

    def job_settings(self, job_id: str) -> AppSettings:
        job_dir = self.job_dir(job_id)
        result = replace(
            self.settings,
            data_dir=job_dir / "data",
            output_dir=job_dir / "output",
        )
        result.ensure_directories()
        return result

    def database(self, job_id: str) -> ReviewDatabase:
        settings = self.job_settings(job_id)
        return ReviewDatabase(settings.data_dir / "docreview.db")

    def export_workbook(self, job_id: str) -> Path:
        with self.lock:
            export_lock = self.export_locks.setdefault(job_id, threading.Lock())
        with export_lock:
            return export_review_workbook(
                self.database(job_id),
                self.job_settings(job_id),
                f"document_review_{job_id}.xlsx",
            )

    def _write_record(self, record: dict) -> None:
        target = self.job_dir(str(record["id"])) / "job.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)


def create_server_app(
    settings: AppSettings,
    storage_root: Path,
    limits: ServerLimits | None = None,
):
    try:
        from fastapi import FastAPI, Form, HTTPException, Request
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
        from starlette.concurrency import run_in_threadpool
    except ImportError as exc:
        raise RuntimeError(
            "服务器模式依赖未安装，请执行: python -m pip install -e '.[server]'"
        ) from exc

    limits = limits or ServerLimits()
    manager = ServerJobManager(settings, storage_root)
    app = FastAPI(title="DocReview Server", docs_url=None, redoc_url=None)
    app.state.job_manager = manager

    username = os.environ.get("DOCREVIEW_USERNAME", "").strip()
    password = os.environ.get("DOCREVIEW_PASSWORD", "")
    session_secret = os.environ.get("DOCREVIEW_SESSION_SECRET", "") or secrets.token_urlsafe(48)
    session_ttl = max(1, int(os.environ.get("DOCREVIEW_SESSION_TTL_HOURS", "12"))) * 3600
    cookie_secure = os.environ.get("DOCREVIEW_COOKIE_SECURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    allowed_origins = {
        value.strip().rstrip("/")
        for value in os.environ.get("DOCREVIEW_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }

    def authenticated(request: Request) -> bool:
        if not (username and password):
            return True
        if _valid_session_cookie(
            request.cookies.get("docreview_session", ""),
            username,
            session_secret,
        ):
            return True
        supplied = request.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        return secrets.compare_digest(supplied, expected)

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        public_path = request.url.path in {"/healthz", "/readyz", "/favicon.ico", "/login"}
        if not public_path and not authenticated(request):
            if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                return RedirectResponse("/login", status_code=303)
            return HTMLResponse("请先登录", status_code=401)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            host = request.headers.get("Host", "")
            normalized_origin = origin.rstrip("/") if origin else ""
            same_host = bool(origin) and urlparse(origin).netloc.lower() == host.lower()
            if origin and not same_host and normalized_origin not in allowed_origins:
                return HTMLResponse("拒绝跨站请求", status_code=403)
        if request.method == "POST" and request.url.path == "/jobs":
            content_length = request.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > limits.max_upload_bytes + 2 * 1024 * 1024:
                        return HTMLResponse("上传内容超过服务器限制", status_code=413)
                except ValueError:
                    return HTMLResponse("无效 Content-Length", status_code=400)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        )
        return response

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready", "storage": str(manager.storage_root)}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return HTMLResponse("", status_code=204)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if authenticated(request) and username and password:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_page())

    @app.post("/login")
    async def login(
        login_username: Annotated[str, Form()],
        login_password: Annotated[str, Form()],
    ):
        valid = bool(username and password) and secrets.compare_digest(
            login_username, username
        ) and secrets.compare_digest(login_password, password)
        if not valid:
            return HTMLResponse(_login_page("用户名或密码错误"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "docreview_session",
            _create_session_cookie(username, session_secret, session_ttl),
            max_age=session_ttl,
            httponly=True,
            secure=cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("docreview_session", path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return HTMLResponse(_home_page(manager, limits))

    @app.post("/jobs")
    async def create_job(request: Request):
        form = await request.form(
            max_files=limits.max_files,
            max_fields=limits.max_files + 10,
            max_part_size=limits.max_upload_bytes,
        )
        files = list(form.getlist("files"))
        relative_paths = [str(value) for value in form.getlist("relative_paths")]
        keywords = str(form.get("keywords", ""))
        task_name = str(form.get("task_name", ""))
        rules = parse_keyword_text(keywords)
        if not rules:
            raise HTTPException(status_code=400, detail="至少输入一个关键词")
        if not files:
            raise HTTPException(status_code=400, detail="请选择文件或文件夹")
        if any(not hasattr(upload, "read") for upload in files):
            raise HTTPException(status_code=400, detail="上传内容不是有效文件")
        if len(files) > limits.max_files:
            raise HTTPException(
                status_code=413,
                detail=f"单次最多上传 {limits.max_files} 个文件",
            )
        if len(relative_paths) != len(files):
            raise HTTPException(status_code=400, detail="上传文件路径信息不完整")

        record = manager.create_record(task_name, keywords)
        upload_root = manager.upload_dir(record["id"])
        total_bytes = 0
        written_paths: set[Path] = set()
        try:
            for upload, raw_path in zip(files, relative_paths):
                relative_path = _safe_relative_path(raw_path or upload.filename or "")
                destination = upload_root / relative_path
                if destination in written_paths:
                    raise ValueError(f"上传中存在重复路径: {relative_path.as_posix()}")
                written_paths.add(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > limits.max_upload_bytes:
                            raise ValueError(
                                f"单次上传不能超过 {_human_bytes(limits.max_upload_bytes)}"
                            )
                        handle.write(chunk)
                await upload.close()
            manager.finish_upload(record["id"], len(files), total_bytes)
        except Exception as exc:
            manager.fail_upload(record["id"], str(exc))
            shutil.rmtree(upload_root, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {"job_id": record["id"], "job_url": f"/jobs/{record['id']}"},
            status_code=201,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_dashboard(job_id: str, page: int = 1):
        record = _require_job(manager, job_id)
        return HTMLResponse(_job_page(manager, record, page))

    @app.get("/jobs/{job_id}/status.json")
    async def job_status(job_id: str):
        record = _require_job(manager, job_id)
        counts = manager.database(job_id).counts()
        return {**record, "counts": counts}

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    async def review_page(job_id: str, id: int, page: int = 1):
        _require_job(manager, job_id)
        item = manager.database(job_id).get_match(id)
        if not item:
            raise HTTPException(status_code=404, detail="记录不存在")
        return HTMLResponse(_server_review_page(manager, job_id, item, max(1, page)))

    @app.post("/jobs/{job_id}/review")
    async def save_review(
        job_id: str,
        id: Annotated[int, Form()],
        status: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
        return_page: Annotated[int, Form()] = 1,
    ):
        _require_job(manager, job_id)
        manager.database(job_id).update_review(id, status, note)
        query = urlencode({"id": id, "page": max(1, return_page)})
        return RedirectResponse(f"/jobs/{job_id}/review?{query}", status_code=303)

    @app.get("/jobs/{job_id}/asset")
    async def asset(job_id: str, id: int, kind: str = "crop"):
        _require_job(manager, job_id)
        item = manager.database(job_id).get_match(id)
        if not item:
            raise HTTPException(status_code=404, detail="记录不存在")
        key = "annotated_path" if kind == "annotated" else "crop_path"
        path = Path(str(item[key])).resolve()
        data_root = manager.job_settings(job_id).data_dir.resolve()
        try:
            path.relative_to(data_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="禁止访问该文件") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="证据图不存在")
        return FileResponse(path)

    @app.get("/jobs/{job_id}/export")
    async def export(job_id: str):
        record = _require_job(manager, job_id)
        if record.get("status") in {"uploading", "queued", "running"}:
            raise HTTPException(status_code=409, detail="分析尚未完成")
        output = await run_in_threadpool(manager.export_workbook, job_id)
        return FileResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"document_review_{job_id}.xlsx",
        )

    return app


def run_production_server(
    settings: AppSettings,
    storage_root: Path,
    host: str = "0.0.0.0",
    port: int = 8765,
    max_upload_mb: int = 2048,
    max_files: int = 2_000,
) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "服务器模式依赖未安装，请执行: python -m pip install -e '.[server]'"
        ) from exc
    app = create_server_app(
        settings,
        storage_root,
        ServerLimits(max_upload_bytes=max_upload_mb * 1024 * 1024, max_files=max_files),
    )
    if not (
        os.environ.get("DOCREVIEW_USERNAME", "").strip()
        and os.environ.get("DOCREVIEW_PASSWORD", "")
    ):
        print("[server] 警告：未配置 DOCREVIEW_USERNAME/DOCREVIEW_PASSWORD，页面当前无登录保护")
    elif not os.environ.get("DOCREVIEW_SESSION_SECRET", ""):
        print("[server] 警告：未配置 DOCREVIEW_SESSION_SECRET，服务重启后登录会话将失效")
    uvicorn.run(app, host=host, port=port, workers=1, proxy_headers=True)


def _home_page(manager: ServerJobManager, limits: ServerLimits) -> str:
    recent_rows = "".join(
        f"""<tr><td><a href="/jobs/{h(item['id'])}">{h(item['name'])}</a></td>
        <td><span class="status {_server_status_class(item.get('status', ''))}">{h(_status_label(item.get('status', '')))}</span></td>
        <td>{int(item.get('file_count', 0))}</td><td>{h(item.get('created_at', '')[:19].replace('T', ' '))}</td></tr>"""
        for item in manager.list_recent()
    ) or '<tr><td colspan="4" class="empty">尚无服务器分析任务</td></tr>'
    body = f"""
    <header><div><p class="eyebrow">DOCREVIEW SERVER</p><h1>服务器文档分析</h1>
    <p>上传文件或整个文件夹，OCR、关键词匹配和证据定位由云服务器完成。</p></div>{_logout_form()}</header>
    <section class="panel upload-panel">
      <div class="section-title"><div><h2>新建分析任务</h2><p>文件夹结构会被保留，便于回溯原始文件。</p></div><span>GPU 任务顺序执行</span></div>
      <form id="upload-form" class="analysis-form">
        <label>任务名称（可选）<input name="task_name" maxlength="100" placeholder="例如：7 月合同合规检查"></label>
        <div class="upload-grid">
          <label class="upload-zone"><span class="upload-icon" aria-hidden="true">＋</span><strong>选择文件</strong><span>可同时选择多个 Word、PDF 或图片</span><input id="file-input" type="file" multiple></label>
          <label class="upload-zone"><span class="upload-icon folder-icon" aria-hidden="true">⌑</span><strong>选择文件夹</strong><span>上传文件夹及其子目录</span><input id="folder-input" type="file" webkitdirectory directory multiple></label>
        </div>
        <div id="file-summary" class="selection-summary">尚未选择文件</div>
        <label>关键词（每行一个；正则以 re: 开头）
          <textarea name="keywords" rows="6" required placeholder="backdoor&#10;evaluation&#10;re:重大.{{0,8}}风险"></textarea>
        </label>
        <div class="form-actions"><span>单次最多 {limits.max_files} 个文件、{_human_bytes(limits.max_upload_bytes)}</span>
          <button class="button" type="submit">上传并开始分析</button></div>
        <div id="upload-progress" class="notice hidden"></div>
      </form>
    </section>
    <section class="panel"><div class="section-title"><div><h2>最近任务</h2><p>每个任务拥有独立数据和审核结果。</p></div></div>
      <div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>文件数</th><th>创建时间</th></tr></thead>
      <tbody>{recent_rows}</tbody></table></div>
    </section>
    <script>{UPLOAD_SCRIPT}</script>
    <style>{SERVER_CSS}</style>
    """
    return layout("服务器文档分析", body)


def _job_page(manager: ServerJobManager, record: dict, requested_page: int) -> str:
    job_id = str(record["id"])
    db = manager.database(job_id)
    counts = db.counts()
    total_matches = counts.get("matches", 0)
    total_pages = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, requested_page), total_pages)
    offset = (page - 1) * PAGE_SIZE
    matches = db.list_matches(PAGE_SIZE, offset)
    documents = db.list_documents()
    status = str(record.get("status", ""))
    refresh = '<meta http-equiv="refresh" content="3">' if status in {"queued", "running"} else ""
    current = int(record.get("current", 0))
    total = int(record.get("total", 0))
    percent = int(100 * current / max(1, total)) if total else 0
    progress = ""
    if status in {"uploading", "queued", "running"}:
        progress = f'<div class="notice"><strong>{h(_status_label(status))}</strong> {percent}% · {h(record.get("message", ""))}<div class="bar"><span style="width:{percent}%"></span></div></div>'
    elif status == "error":
        progress = f'<div class="error"><strong>任务失败：</strong>{h(record.get("error", ""))}</div>'

    cards = "".join(
        f'<div class="card"><span>{h(label)}</span><strong>{counts.get(key, 0)}</strong></div>'
        for label, key in [
            ("文件", "documents"),
            ("关键词命中", "matches"),
            ("待审核", "待审核"),
            ("有问题", "有问题"),
            ("不支持", "unsupported"),
            ("处理错误", "errors"),
        ]
    )
    rows = "".join(
        f"""<tr><td><span class="status {status_class(item['review_status'])}">{h(item['review_status'])}</span></td>
        <td>{h(item['keyword'])}</td><td class="filename" title="{h(_display_source(manager, job_id, item['source_path']))}"><span>{h(_display_source(manager, job_id, item['source_path']))}</span></td>
        <td class="page-number">{item['page_no']}</td><td class="excerpt">{h(shorten(item['text'], 180))}</td>
        <td><a class="button small" href="/jobs/{job_id}/review?{urlencode({'id': item['id'], 'page': page})}">审核证据</a></td></tr>"""
        for item in matches
    ) or '<tr><td colspan="6" class="empty">尚无关键词命中记录</td></tr>'
    first_item = offset + 1 if total_matches else 0
    last_item = min(offset + len(matches), total_matches)
    problems = "".join(
        f"<tr><td>{h(_display_source(manager, job_id, item['source_path']))}</td><td>{h(item['extension'])}</td><td>{h(item['status'])}</td><td>{h(item['message'])}</td></tr>"
        for item in documents
        if item["status"] in {"unsupported", "error"}
    ) or '<tr><td colspan="4" class="empty">没有不支持或失败的文件</td></tr>'
    export_disabled = status in {"uploading", "queued", "running"}
    export_action = (
        '<span class="button disabled-button">分析完成后可导出</span>'
        if export_disabled
        else f'<a class="button" href="/jobs/{job_id}/export">下载全部审核数据 (.xlsx)</a>'
    )
    retention_note = (
        "原始文件已从服务器删除"
        if record.get("source_files_deleted")
        else "分析结束后自动删除原始文件"
        if manager.delete_uploads_after_analysis and status in {"uploading", "queued", "running"}
        else ""
    )
    retention_text = f" · {h(retention_note)}" if retention_note else ""
    body = f"""
    {refresh}
    <header><div><p class="eyebrow">SERVER JOB · {h(job_id)}</p><h1>{h(record['name'])}</h1>
      <p>{int(record.get('file_count', 0))} 个上传文件 · {_human_bytes(int(record.get('upload_bytes', 0)))} · {h(_status_label(status))}{retention_text}</p></div>
      <div class="header-actions"><a class="button secondary" href="/">新建或查看其他任务</a>{_logout_form()}</div></header>
    {progress}<section class="cards">{cards}</section>
    <section class="panel export-strip"><div><h2>审核与归档</h2><p>页面用于逐条核对；Excel 会导出本任务全部命中、来源路径、备注和证据截图。</p></div>{export_action}</section>
    <section class="panel evidence-panel"><div class="section-title"><div><h2>命中证据</h2><p>逐条核对上传相对路径、上下文和页面位置。</p></div><span>第 {first_item}–{last_item} 条，共 {total_matches} 条</span></div>
      <div class="table-wrap"><table><thead><tr><th>状态</th><th>关键词</th><th>上传路径</th><th>页码</th><th>段落/图片文字</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>
      {_server_pagination(job_id, page, total_pages, total_matches)}
    </section>
    <section class="panel"><h2>不支持或处理失败</h2><div class="table-wrap"><table><thead><tr><th>上传路径</th><th>扩展名</th><th>状态</th><th>提示</th></tr></thead><tbody>{problems}</tbody></table></div></section>
    <style>{SERVER_CSS}</style>
    """
    return layout(str(record["name"]), body)


def _server_review_page(
    manager: ServerJobManager, job_id: str, item: dict, return_page: int
) -> str:
    match_id = int(item["id"])
    options = "".join(
        f'<option value="{h(status)}" {"selected" if status == item["review_status"] else ""}>{h(status)}</option>'
        for status in ["待审核", "正常", "有问题", "待确认", "误识别"]
    )
    source = h(_display_source(manager, job_id, item["source_path"]))
    body = f"""
    <header><div><p class="eyebrow">SERVER EVIDENCE #{match_id}</p><h1>{h(item['keyword'])}</h1>
    <p>{source} · 第 {item['page_no']} 页 · {h(item['kind'])}</p></div>
    <div class="header-actions"><a class="button secondary" href="/jobs/{job_id}?page={return_page}">返回第 {return_page} 页</a>{_logout_form()}</div></header>
    <div class="review-grid"><section class="panel evidence"><h2>页面定位</h2>
      <img src="/jobs/{job_id}/asset?id={match_id}&kind=annotated" alt="标注后的页面"></section>
      <section class="panel review-form"><h2>命中内容</h2><div class="keyword-chip">{h(item['keyword'])}</div>
      <pre>{highlight_text(item['text'], item['matched_text'] or item['keyword'])}</pre>
      <dl><dt>上传路径</dt><dd>{source}</dd><dt>页码</dt><dd>{item['page_no']}</dd>
      <dt>位置</dt><dd>({item['x0']:.4f}, {item['y0']:.4f}, {item['x1']:.4f}, {item['y1']:.4f})</dd>
      <dt>识别置信度</dt><dd>{item['confidence']:.1%}</dd><dt>SHA-256</dt><dd class="hash">{h(item['sha256'])}</dd></dl>
      <form method="post" action="/jobs/{job_id}/review"><input type="hidden" name="id" value="{match_id}">
      <input type="hidden" name="return_page" value="{return_page}"><label>审核结论<select name="status">{options}</select></label>
      <label>备注<textarea name="note" rows="5">{h(item['note'])}</textarea></label><button class="button" type="submit">保存审核结果</button></form>
      </section></div><style>{SERVER_CSS}</style>
    """
    return layout(f"审核 #{match_id}", body)


def _server_pagination(job_id: str, page: int, total_pages: int, total: int) -> str:
    if total_pages <= 1:
        return ""
    visible = {1, total_pages, *range(max(1, page - 2), min(total_pages, page + 2) + 1)}
    links: list[str] = []
    previous = 0
    for number in sorted(visible):
        if previous and number - previous > 1:
            links.append('<span class="page-ellipsis">…</span>')
        if number == page:
            links.append(f'<span class="page-link current">{number}</span>')
        else:
            links.append(f'<a class="page-link" href="/jobs/{job_id}?page={number}">{number}</a>')
        previous = number
    previous_link = max(1, page - 1)
    next_link = min(total_pages, page + 1)
    return f"""<nav class="pagination"><span class="page-summary">每页 {PAGE_SIZE} 条 · 共 {total} 条</span><div class="page-actions">
    <a class="page-link" href="/jobs/{job_id}?page=1">首页</a><a class="page-link" href="/jobs/{job_id}?page={previous_link}">上一页</a>
    {''.join(links)}<a class="page-link" href="/jobs/{job_id}?page={next_link}">下一页</a><a class="page-link" href="/jobs/{job_id}?page={total_pages}">末页</a></div></nav>"""


def _login_page(error: str = "") -> str:
    error_html = f'<div class="error">{h(error)}</div>' if error else ""
    body = f"""
    <div class="login-shell"><section class="login-card">
      <div class="login-intro"><div class="brand-mark" aria-hidden="true">D</div>
        <p class="eyebrow">DOCREVIEW SERVER</p><h1>文档证据<br>审核平台</h1>
        <p>统一分析 Word、PDF 与图片，快速定位关键词所在段落和页面证据。</p>
        <div class="login-feature"><span>01</span><p><strong>文档统一解析</strong><small>原生文本与 OCR 图像内容</small></p></div>
        <div class="login-feature"><span>02</span><p><strong>证据精准回溯</strong><small>保留页码、位置与来源路径</small></p></div>
      </div>
      <div class="login-form-panel"><p class="eyebrow">SECURE ACCESS</p><h2>欢迎回来</h2>
        <p>请使用管理员分配的账户登录。</p>{error_html}
        <form method="post" action="/login" class="analysis-form">
          <label>用户名<input name="login_username" autocomplete="username" placeholder="请输入用户名" required autofocus></label>
          <label>密码<input name="login_password" type="password" autocomplete="current-password" placeholder="请输入密码" required></label>
          <button class="button" type="submit">安全登录</button>
        </form><small class="security-note">仅授权用户可访问上传文件、审核记录和导出数据</small>
      </div>
    </section></div><style>{SERVER_CSS}</style>
    """
    return layout("登录 · 服务器文档分析", body)


def _logout_form() -> str:
    return '<form method="post" action="/logout" class="logout-form"><button class="button secondary" type="submit">退出登录</button></form>'


def _require_job(manager: ServerJobManager, job_id: str) -> dict:
    record = manager.get(job_id)
    if not record:
        try:
            from fastapi import HTTPException
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("服务器依赖未安装") from exc
        raise HTTPException(status_code=404, detail="任务不存在")
    return record


def _safe_relative_path(value: str) -> Path:
    normalized = value.replace("\\", "/").strip().lstrip("/")
    posix = PurePosixPath(normalized)
    parts = [part for part in posix.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("上传文件路径无效")
    if len(parts) > 32 or len(normalized) > 500:
        raise ValueError("上传文件路径过长")
    if any("\x00" in part for part in parts):
        raise ValueError("上传文件路径包含非法字符")
    return Path(*parts)


def _create_session_cookie(username: str, secret: str, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    payload = f"{username}\n{expires}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac_module.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def _valid_session_cookie(token: str, username: str, secret: str) -> bool:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac_module.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac_module.compare_digest(signature, expected):
            return False
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        cookie_username, expires_text = payload.rsplit("\n", 1)
        return hmac_module.compare_digest(cookie_username, username) and int(
            expires_text
        ) >= int(time.time())
    except (ValueError, UnicodeError):
        return False


def _valid_job_id(job_id: str) -> bool:
    return len(job_id) == 20 and all(character in "0123456789abcdef" for character in job_id)


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _display_source(manager: ServerJobManager, job_id: str, source_path: str) -> str:
    try:
        return Path(source_path).resolve().relative_to(manager.upload_dir(job_id)).as_posix()
    except ValueError:
        return Path(source_path).name


def _status_label(status: str) -> str:
    return {
        "uploading": "上传中",
        "queued": "排队中",
        "running": "分析中",
        "complete": "已完成",
        "error": "失败",
    }.get(status, status or "未知")


def _server_status_class(status: str) -> str:
    return {
        "complete": "good",
        "error": "bad",
        "queued": "warn",
        "running": "pending",
        "uploading": "pending",
    }.get(status, "muted")


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


UPLOAD_SCRIPT = r"""
const form = document.getElementById('upload-form');
const fileInput = document.getElementById('file-input');
const folderInput = document.getElementById('folder-input');
const summary = document.getElementById('file-summary');
const progress = document.getElementById('upload-progress');
function selectedFiles() { return [...fileInput.files, ...folderInput.files]; }
function updateSummary() {
  const files = selectedFiles();
  const bytes = files.reduce((total, file) => total + file.size, 0);
  summary.textContent = files.length ? `${files.length} 个文件 · ${(bytes / 1024 / 1024).toFixed(1)} MB` : '尚未选择文件';
}
fileInput.addEventListener('change', updateSummary);
folderInput.addEventListener('change', updateSummary);
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = selectedFiles();
  if (!files.length) { alert('请选择文件或文件夹'); return; }
  const button = form.querySelector('button[type=submit]');
  const data = new FormData();
  data.append('task_name', form.elements.task_name.value);
  data.append('keywords', form.elements.keywords.value);
  for (const file of files) {
    data.append('files', file, file.name);
    data.append('relative_paths', file.webkitRelativePath || file.name);
  }
  button.disabled = true;
  progress.classList.remove('hidden');
  progress.textContent = '正在上传文件，请勿关闭页面…';
  try {
    const response = await fetch('/jobs', { method: 'POST', body: data });
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : { detail: await response.text() };
    if (!response.ok) throw new Error(payload.detail || '上传失败');
    window.location.href = payload.job_url;
  } catch (error) {
    progress.className = 'error';
    progress.textContent = error.message;
    button.disabled = false;
  }
});
"""


SERVER_CSS = """
.upload-panel{border-top:4px solid var(--teal)}.upload-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.upload-zone{position:relative;display:flex;min-height:156px;flex-direction:column;align-items:center;justify-content:center;padding:24px;border:1.5px dashed #8ba7b7;border-radius:14px;background:linear-gradient(145deg,#f8fbfc,#f1f7f8);cursor:pointer;text-align:center;transition:.18s ease}.upload-zone:hover{border-color:var(--teal);background:#f0fbf9;transform:translateY(-1px);box-shadow:0 10px 24px rgba(15,139,141,.09)}
.upload-zone input{position:absolute;width:1px;height:1px;margin:0;padding:0;opacity:0;pointer-events:none}.upload-zone strong{display:block;margin:8px 0 3px;color:var(--navy);font-size:14px}.upload-zone>span:not(.upload-icon){display:block;color:var(--muted);font-size:12px;font-weight:450}.upload-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:#fff;color:var(--teal);font-size:23px;font-weight:400;box-shadow:0 7px 18px rgba(16,42,67,.09)}.folder-icon{font-size:25px}
.selection-summary{padding:11px 13px;border:1px solid #deeaef;border-radius:10px;background:var(--soft);color:var(--navy);font-weight:750;font-size:13px}
.hidden{display:none}.export-strip{display:flex;align-items:center;justify-content:space-between;gap:20px;border-left:4px solid var(--teal);background:linear-gradient(110deg,#fff 0%,#f3fbfa 100%)}
.disabled-button{opacity:.5;cursor:not-allowed}.disabled-button:hover{transform:none;background:var(--teal)}
.header-actions{display:flex;gap:10px;align-items:center}.logout-form{margin:0}.login-shell{min-height:calc(100vh - 104px);display:grid;place-items:center}.login-card{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(360px,1.1fr);width:min(880px,100%);overflow:hidden;border:1px solid rgba(255,255,255,.6);border-radius:24px;background:#fff;box-shadow:0 28px 80px rgba(16,42,67,.16)}
.login-intro{position:relative;padding:46px 42px;background:linear-gradient(145deg,#102a43,#16445a 65%,#0c7374);overflow:hidden}.login-intro:after{content:"";position:absolute;right:-100px;bottom:-110px;width:310px;height:310px;border:1px solid rgba(255,255,255,.13);border-radius:50%;box-shadow:0 0 0 45px rgba(255,255,255,.025),0 0 0 90px rgba(255,255,255,.018)}.login-intro>*{position:relative;z-index:1}.brand-mark{display:grid;place-items:center;width:46px;height:46px;margin-bottom:35px;border:1px solid rgba(255,255,255,.24);border-radius:14px;background:rgba(255,255,255,.1);color:#fff;font-size:21px;font-weight:850;box-shadow:inset 0 1px 0 rgba(255,255,255,.18)}.login-intro .eyebrow{color:#78ddd6}.login-intro h1{margin:8px 0 14px;color:#fff;font-size:38px}.login-intro>p:not(.eyebrow){max-width:330px;margin-bottom:30px;color:#c8d9e3}.login-feature{display:flex;align-items:center;gap:12px;margin-top:15px}.login-feature>span{display:grid;place-items:center;width:31px;height:31px;border-radius:9px;background:rgba(255,255,255,.1);color:#8de3dc;font-size:10px;font-weight:850}.login-feature p{display:grid;color:#fff;line-height:1.35}.login-feature strong{font-size:12px}.login-feature small{margin-top:2px;color:#afc8d4;font-size:11px}
.login-form-panel{display:flex;flex-direction:column;justify-content:center;padding:52px 54px}.login-form-panel h2{margin:4px 0 6px;font-size:28px}.login-form-panel>p:not(.eyebrow){margin-bottom:28px}.login-form-panel .analysis-form{gap:17px}.login-form-panel .button{width:100%;margin-top:5px}.login-form-panel .error{margin:-12px 0 22px;box-shadow:none}.security-note{display:block;margin-top:18px;color:#8293a1;font-size:11px;text-align:center}
@media(max-width:700px){.upload-grid{grid-template-columns:1fr}.upload-zone{min-height:130px}.export-strip{align-items:flex-start;flex-direction:column}.export-strip .button{width:100%}.header-actions{align-items:stretch;flex-direction:column}.header-actions .button,.header-actions form,.header-actions form .button{width:100%}.login-shell{min-height:calc(100vh - 28px)}.login-card{grid-template-columns:1fr;border-radius:18px}.login-intro{padding:28px 24px}.brand-mark{margin-bottom:22px}.login-intro h1{font-size:30px}.login-feature{display:none}.login-form-panel{padding:30px 24px 32px}}
"""

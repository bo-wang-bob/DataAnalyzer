from __future__ import annotations

import html
import mimetypes
import re
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .db import ReviewDatabase
from .exporter import export_review_workbook
from .matching import parse_keyword_text
from .models import AppSettings
from .service import Analyzer


PAGE_SIZE = 50


@dataclass
class JobState:
    running: bool = False
    current: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "current": self.current,
                "total": self.total,
                "message": self.message,
                "error": self.error,
            }


class WebApplication:
    def __init__(self, settings: AppSettings, db: ReviewDatabase):
        self.settings = settings
        self.db = db
        self.analyzer = Analyzer(settings, db)
        self.job = JobState()

    def start_job(self, source_dir: Path, keyword_text: str) -> None:
        rules = parse_keyword_text(keyword_text)
        if not rules:
            raise ValueError("至少输入一个关键词")
        with self.job.lock:
            if self.job.running:
                raise ValueError("当前已有分析任务在运行")
            self.job.running = True
            self.job.current = 0
            self.job.total = 0
            self.job.message = "正在扫描文件"
            self.job.error = ""

        def run() -> None:
            try:
                self.analyzer.analyze_directory(
                    source_dir,
                    rules,
                    progress=self._update_progress,
                )
            except Exception as exc:
                with self.job.lock:
                    self.job.error = str(exc)
                    self.job.message = "分析失败"
            finally:
                with self.job.lock:
                    self.job.running = False

        threading.Thread(target=run, daemon=True).start()

    def _update_progress(self, current: int, total: int, message: str) -> None:
        with self.job.lock:
            self.job.current = current
            self.job.total = total
            self.job.message = message


def run_server(
    settings: AppSettings,
    db: ReviewDatabase,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    application = WebApplication(settings, db)
    handler = _handler_factory(application)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"文档审核系统已启动: http://{host}:{port}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_factory(application: WebApplication):
    class Handler(BaseHTTPRequestHandler):
        app = application

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self._send_html(self._dashboard(query))
            elif parsed.path == "/review":
                self._send_html(
                    self._review_page(
                        _int_query(query, "id"),
                        max(1, _int_query(query, "page")),
                    )
                )
            elif parsed.path == "/asset":
                self._send_asset(_int_query(query, "id"), query.get("kind", ["crop"])[0])
            elif parsed.path == "/export":
                self._download_export()
            elif parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            values = parse_qs(body)
            if self.path == "/start":
                try:
                    source = Path(values.get("source_dir", ["datas"])[0])
                    if not source.is_absolute():
                        source = self.app.settings.root_dir / source
                    self.app.start_job(source, values.get("keywords", [""])[0])
                    self._redirect("/")
                except Exception as exc:
                    self._send_html(self._message_page("无法开始分析", str(exc)), status=400)
            elif self.path == "/review":
                try:
                    match_id = int(values.get("id", ["0"])[0])
                    status = values.get("status", ["待审核"])[0]
                    note = values.get("note", [""])[0]
                    return_page = max(1, int(values.get("return_page", ["1"])[0]))
                    self.app.db.update_review(match_id, status, note)
                    self._redirect(
                        f"/review?{urlencode({'id': match_id, 'page': return_page})}"
                    )
                except Exception as exc:
                    self._send_html(self._message_page("保存失败", str(exc)), status=400)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _dashboard(self, query: dict) -> str:
            counts = self.app.db.counts()
            total_matches = counts.get("matches", 0)
            total_pages = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(max(1, _int_query(query, "page")), total_pages)
            offset = (page - 1) * PAGE_SIZE
            matches = self.app.db.list_matches(limit=PAGE_SIZE, offset=offset)
            documents = self.app.db.list_documents()
            job = self.app.job.snapshot()
            progress = ""
            refresh = ""
            if job["running"]:
                percent = int(100 * job["current"] / max(1, job["total"]))
                progress = f"""
                <div class="notice"><strong>分析运行中</strong> {percent}% · {h(job['message'])}
                <div class="bar"><span style="width:{percent}%"></span></div></div>"""
                refresh = '<meta http-equiv="refresh" content="3">'
            elif job["error"]:
                progress = f'<div class="error"><strong>最近任务失败：</strong>{h(job["error"])}</div>'

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
                f"""
                <tr>
                  <td><span class="status {status_class(item['review_status'])}">{h(item['review_status'])}</span></td>
                  <td>{h(item['keyword'])}</td>
                  <td class="filename" title="{h(item['source_path'])}">{h(item['filename'])}</td>
                  <td class="page-number">{item['page_no']}</td>
                  <td class="excerpt">{h(shorten(item['text'], 180))}</td>
                  <td><a class="button small" href="/review?{urlencode({'id': item['id'], 'page': page})}">审核证据</a></td>
                </tr>"""
                for item in matches
            ) or '<tr><td colspan="6" class="empty">尚无关键词命中记录</td></tr>'
            first_item = offset + 1 if total_matches else 0
            last_item = min(offset + len(matches), total_matches)
            pagination = pagination_controls(page, total_pages, total_matches)
            problem_rows = "".join(
                f"<tr><td>{h(item['filename'])}</td><td>{h(item['extension'])}</td><td>{h(item['status'])}</td><td>{h(item['message'])}</td></tr>"
                for item in documents
                if item["status"] in {"unsupported", "error"}
            ) or '<tr><td colspan="4" class="empty">没有不支持或失败的文件</td></tr>'
            return layout(
                "文档关键词审核",
                f"""
                {refresh}
                <header><div><p class="eyebrow">LOCAL EVIDENCE REVIEW</p><h1>文档关键词审核</h1>
                <p>Word、原生/扫描 PDF、图片统一定位，保留原文件、页码和坐标。</p></div></header>
                {progress}
                <section class="cards">{cards}</section>
                <div class="action-grid">
                  <section class="panel analysis-panel">
                    <div class="section-heading"><div><p class="eyebrow">ANALYZE</p><h2>开始一次分析</h2></div><span class="step-badge">01</span></div>
                    <form method="post" action="/start" class="analysis-form">
                      <label>文件目录<input name="source_dir" value="datas" required></label>
                      <label>关键词（每行一个；正则以 re: 开头）
                        <textarea name="keywords" rows="5" placeholder="风险&#10;异常&#10;re:重大.{{0,8}}风险" required></textarea>
                      </label>
                      <div class="form-actions"><span>支持 Word、PDF 和常见图片格式</span>
                        <button class="button" type="submit" {'disabled' if job['running'] else ''}>开始分析</button></div>
                    </form>
                  </section>
                  <section class="panel export-panel">
                    <div class="section-heading"><div><p class="eyebrow">EXPORT</p><h2>导出审核归档</h2></div><span class="step-badge">02</span></div>
                    <p>无论当前查看哪一页，都会导出数据库中的全部 <strong>{total_matches}</strong> 条命中。</p>
                    <ul class="export-list">
                      <li><strong>审核汇总</strong><span>命中数、审核状态、文件处理概览</span></li>
                      <li><strong>审核明细</strong><span>关键词、段落、页码、坐标、来源路径、审核备注和证据截图</span></li>
                      <li><strong>不支持文件</strong><span>文件名、扩展名、原始路径和不支持原因</span></li>
                    </ul>
                    <a class="button export-button" href="/export">下载全部数据 (.xlsx)</a>
                    <small>Excel 是当前审核状态的归档快照；数据较多时生成可能需要一些时间。</small>
                  </section>
                </div>
                <section class="panel evidence-panel"><div class="section-title"><div><h2>命中证据</h2><p>逐条核对上下文、页面位置和来源文件</p></div>
                  <span>第 {first_item}–{last_item} 条，共 {total_matches} 条</span></div>
                  <div class="table-wrap"><table><thead><tr><th>状态</th><th>关键词</th><th>文件</th><th>页码</th><th>段落/图片文字</th><th></th></tr></thead>
                  <tbody>{rows}</tbody></table></div>
                  {pagination}
                </section>
                <section class="panel"><h2>不支持或处理失败</h2><div class="table-wrap"><table>
                  <thead><tr><th>文件</th><th>扩展名</th><th>状态</th><th>提示</th></tr></thead><tbody>{problem_rows}</tbody>
                </table></div></section>
                """,
            )

        def _review_page(self, match_id: int, return_page: int = 1) -> str:
            item = self.app.db.get_match(match_id)
            if not item:
                return self._message_page("记录不存在", f"没有找到记录 {match_id}")
            options = "".join(
                f'<option value="{h(status)}" {"selected" if status == item["review_status"] else ""}>{h(status)}</option>'
                for status in ["待审核", "正常", "有问题", "待确认", "误识别"]
            )
            source = h(item["source_path"])
            return layout(
                f"审核 #{match_id}",
                f"""
                <header><div><p class="eyebrow">EVIDENCE #{match_id}</p><h1>{h(item['keyword'])}</h1>
                <p>{h(item['filename'])} · 第 {item['page_no']} 页 · {h(item['kind'])}</p></div>
                <a class="button secondary" href="/?page={return_page}">返回第 {return_page} 页</a></header>
                <div class="review-grid">
                  <section class="panel evidence"><h2>页面定位</h2>
                    <img src="/asset?id={match_id}&kind=annotated" alt="标注后的页面">
                  </section>
                  <section class="panel review-form"><h2>命中内容</h2>
                    <div class="keyword-chip">{h(item['keyword'])}</div>
                    <pre>{highlight_text(item['text'], item['matched_text'] or item['keyword'])}</pre>
                    <dl><dt>来源文件</dt><dd>{source}</dd><dt>页码</dt><dd>{item['page_no']}</dd>
                    <dt>位置</dt><dd>({item['x0']:.4f}, {item['y0']:.4f}, {item['x1']:.4f}, {item['y1']:.4f})</dd>
                    <dt>识别置信度</dt><dd>{item['confidence']:.1%}</dd><dt>SHA-256</dt><dd class="hash">{h(item['sha256'])}</dd></dl>
                    <form method="post" action="/review">
                      <input type="hidden" name="id" value="{match_id}">
                      <input type="hidden" name="return_page" value="{return_page}">
                      <label>审核结论<select name="status">{options}</select></label>
                      <label>备注<textarea name="note" rows="5">{h(item['note'])}</textarea></label>
                      <button class="button" type="submit">保存审核结果</button>
                    </form>
                  </section>
                </div>
                """,
            )

        def _send_asset(self, match_id: int, kind: str) -> None:
            item = self.app.db.get_match(match_id)
            if not item:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            key = "annotated_path" if kind == "annotated" else "crop_path"
            path = Path(item[key]).resolve()
            data_root = self.app.settings.data_dir.resolve()
            try:
                path.relative_to(data_root)
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _download_export(self) -> None:
            try:
                path = export_review_workbook(self.app.db, self.app.settings)
            except Exception as exc:
                self._send_html(self._message_page("导出失败", str(exc)), status=500)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _message_page(self, title: str, message: str) -> str:
            return layout(title, f'<section class="panel"><h1>{h(title)}</h1><p>{h(message)}</p><a class="button" href="/">返回</a></section>')

        def _send_html(self, content: str, status: int = 200) -> None:
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            print(f"[web] {self.address_string()} - {format % args}")

    return Handler


def layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{h(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""


def h(value) -> str:
    return html.escape(str(value or ""))


def shorten(value: str, length: int) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


def highlight_text(value: str, matched_text: str) -> str:
    if not matched_text:
        return h(value)
    pattern = re.compile(re.escape(matched_text), re.IGNORECASE)
    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        pieces.append(h(value[cursor : match.start()]))
        pieces.append(f"<mark>{h(match.group(0))}</mark>")
        cursor = match.end()
    pieces.append(h(value[cursor:]))
    return "".join(pieces)


def status_class(value: str) -> str:
    return {"有问题": "bad", "正常": "good", "待确认": "warn", "误识别": "muted"}.get(value, "pending")


def pagination_controls(page: int, total_pages: int, total_items: int) -> str:
    if total_pages <= 1:
        return ""

    def link(label: str, target: int, disabled: bool = False) -> str:
        if disabled:
            return f'<span class="page-link disabled" aria-disabled="true">{label}</span>'
        return f'<a class="page-link" href="/?page={target}">{label}</a>'

    visible = {1, total_pages}
    visible.update(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    page_links: list[str] = []
    previous = 0
    for number in sorted(visible):
        if previous and number - previous > 1:
            page_links.append('<span class="page-ellipsis">…</span>')
        if number == page:
            page_links.append(
                f'<span class="page-link current" aria-current="page">{number}</span>'
            )
        else:
            page_links.append(f'<a class="page-link" href="/?page={number}">{number}</a>')
        previous = number

    return (
        '<nav class="pagination" aria-label="命中记录分页">'
        f'<span class="page-summary">每页 {PAGE_SIZE} 条 · 共 {total_items} 条</span>'
        '<div class="page-actions">'
        f'{link("首页", 1, page == 1)}'
        f'{link("上一页", page - 1, page == 1)}'
        f'{"".join(page_links)}'
        f'{link("下一页", page + 1, page == total_pages)}'
        f'{link("末页", total_pages, page == total_pages)}'
        '</div></nav>'
    )


def _int_query(query: dict, key: str) -> int:
    try:
        return int(query.get(key, ["0"])[0])
    except (TypeError, ValueError):
        return 0


CSS = """
:root{--navy:#17324d;--teal:#0f766e;--teal-dark:#0b5d57;--paper:#f3f6f8;--line:#d8e0e8;--muted:#637180;--soft:#eef3f6}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:#17212b;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1440px;margin:0 auto;padding:38px 30px 72px}
header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;margin-bottom:26px}
h1{font-size:36px;line-height:1.15;margin:4px 0 7px;color:var(--navy);letter-spacing:-.02em}
h2{font-size:19px;margin:0;color:var(--navy)}
p{margin:0;color:var(--muted)}
.eyebrow{font-size:10px;letter-spacing:.18em;font-weight:800;color:var(--teal);margin-bottom:3px}
.button{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:8px;background:var(--teal);color:white;padding:10px 17px;font-weight:700;text-decoration:none;cursor:pointer;transition:.15s ease}
.button:hover{background:var(--teal-dark);transform:translateY(-1px)}
.button.secondary{background:white;color:var(--navy);border:1px solid var(--line)}
.button.small{font-size:13px;padding:6px 10px;white-space:nowrap}
.button:disabled{opacity:.45;cursor:not-allowed;transform:none}
.cards{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:18px}
.card,.panel,.notice,.error{background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 7px 24px rgba(23,50,77,.05)}
.card{padding:15px 16px;border-top:3px solid #dbe7ed}
.card span{display:block;color:var(--muted);font-size:12px}
.card strong{display:block;font-size:28px;line-height:1.2;margin-top:3px;color:var(--navy)}
.panel{padding:22px;margin-bottom:18px}
.action-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(360px,.75fr);gap:18px;align-items:stretch}
.action-grid .panel{height:calc(100% - 18px)}
.analysis-panel{border-top:4px solid var(--navy)}
.export-panel{border-top:4px solid var(--teal);background:linear-gradient(145deg,#fff 0%,#f5fbf9 100%)}
.section-heading,.section-title{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}
.section-heading{margin-bottom:17px}.section-title{align-items:center;margin-bottom:16px}
.section-title h2{margin-bottom:2px}.section-title span{color:var(--muted);font-size:12px;white-space:nowrap}
.step-badge{display:grid;place-items:center;width:32px;height:32px;border-radius:50%;background:var(--soft);color:var(--navy);font-size:11px;font-weight:800}
.analysis-form{display:grid;gap:14px}
label{display:block;font-weight:700;color:var(--navy);font-size:13px}
input,textarea,select{width:100%;margin-top:6px;padding:10px 11px;border:1px solid #bbc7d2;border-radius:7px;background:#fff;color:#17212b;font:inherit;outline:none}
input:focus,textarea:focus,select:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(15,118,110,.12)}
textarea{resize:vertical}.form-actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.form-actions span{color:var(--muted);font-size:12px}
.export-panel>p strong{color:var(--teal-dark)}
.export-list{list-style:none;margin:17px 0;padding:0;border-top:1px solid #dce9e5}
.export-list li{display:grid;grid-template-columns:90px 1fr;gap:12px;padding:10px 0;border-bottom:1px solid #dce9e5}
.export-list strong{color:var(--navy);font-size:13px}.export-list span{color:var(--muted);font-size:12px}
.export-button{width:100%;margin-top:2px}.export-panel small{display:block;color:var(--muted);font-size:11px;margin-top:9px}
.evidence-panel{padding-bottom:16px}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:9px}
table{width:100%;min-width:960px;border-collapse:separate;border-spacing:0}
th{position:sticky;top:0;z-index:1;background:var(--soft);color:#425466;text-align:left;font-size:12px;padding:10px;border-bottom:1px solid var(--line)}
td{padding:12px 10px;border-bottom:1px solid #edf0f2;vertical-align:top;background:#fff}
tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:#f9fbfc}
.filename{max-width:250px;word-break:break-word}.page-number{text-align:center;font-variant-numeric:tabular-nums}.excerpt{max-width:620px;color:#334155}.empty{text-align:center;color:var(--muted);padding:30px}
.status{display:inline-block;padding:3px 8px;border-radius:99px;font-size:12px;font-weight:800;white-space:nowrap}
.status.pending{background:#eaf1f8;color:#315b7d}.status.bad{background:#fee2e2;color:#991b1b}.status.good{background:#dcfce7;color:#166534}.status.warn{background:#fef3c7;color:#92400e}.status.muted{background:#e5e7eb;color:#4b5563}
.pagination{display:flex;align-items:center;justify-content:space-between;gap:18px;padding-top:16px}.page-summary{font-size:12px;color:var(--muted)}
.page-actions{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}.page-link{display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:34px;padding:0 10px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--navy);font-size:12px;font-weight:700;text-decoration:none}
.page-link:hover{border-color:var(--teal);color:var(--teal)}.page-link.current{background:var(--teal);border-color:var(--teal);color:#fff}.page-link.disabled{opacity:.42}.page-ellipsis{padding:6px 2px;color:var(--muted)}
.notice,.error{padding:14px 16px;margin-bottom:18px}.error{border-color:#fecaca;background:#fff7f7;color:#991b1b}.bar{height:6px;background:#e7ecef;border-radius:99px;margin-top:9px;overflow:hidden}.bar span{display:block;height:100%;background:var(--teal)}
.review-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(360px,.75fr);gap:18px}.evidence img{display:block;max-width:100%;max-height:80vh;margin:auto;border:1px solid var(--line)}.review-form pre{white-space:pre-wrap;background:#f6f8fa;border-radius:8px;padding:15px;font:14px/1.65 inherit}.review-form mark{background:#fde68a;color:#713f12;border-radius:3px;padding:0 2px}.keyword-chip{display:inline-block;background:#ddf4ef;color:#155e55;padding:5px 10px;border-radius:99px;font-weight:800;margin-bottom:10px}
dl{display:grid;grid-template-columns:100px 1fr;gap:8px 12px;font-size:13px}dt{font-weight:700;color:var(--muted)}dd{margin:0;word-break:break-all}.hash{font-family:ui-monospace,monospace;font-size:11px}.review-form form{border-top:1px solid var(--line);padding-top:16px}.review-form form label{margin-bottom:13px}
@media(max-width:1050px){.cards{grid-template-columns:repeat(3,1fr)}.action-grid,.review-grid{grid-template-columns:1fr}.action-grid .panel{height:auto}}
@media(max-width:700px){main{padding:24px 14px 52px}.cards{grid-template-columns:repeat(2,1fr)}header{display:block}.form-actions,.pagination{align-items:stretch;flex-direction:column}.form-actions .button{width:100%}.page-actions{justify-content:flex-start}.section-title{align-items:flex-start;flex-direction:column}.export-list li{grid-template-columns:1fr;gap:2px}}
"""

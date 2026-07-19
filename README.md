# DocReview - 本地文档关键词证据审核

DocReview 将 Word、原生/扫描/混合 PDF 和图片统一转换成“文本 + 页码 + 坐标”的证据块，匹配关键词后生成带红框的页面证据，并在本地页面中完成审核和来源回溯。

项目提供两种运行模式：本地版直接分析当前电脑上的目录；服务器版由用户通过网站上传文件或整个文件夹，再由 Linux 云服务器使用 CPU 或 NVIDIA GPU 分析。

## 第一版范围

支持：

- Word：`.doc`、`.docx`。Windows 优先调用已安装的 Microsoft Word 导出 PDF，LibreOffice 作为可选备用；其他系统使用 LibreOffice。固定版面后，正文和内嵌图片都能保留页码。
- PDF：原生文字、扫描页、混合型 PDF；逐页判定是否需要 OCR。
- 图片：`.png`、`.jpg`、`.jpeg`、`.tif`、`.tiff`、`.bmp`、`.webp`。
- 原生 PDF 和 Word 内嵌的栅格图片：单独裁切 OCR，再映射回所在页坐标。

其他扩展名不会被静默忽略，而会进入“不支持文件”清单。

本地模式不会上传文件。服务器模式只把文件上传到用户自行部署的服务器，并按任务隔离存储。macOS 的 `auto` 模式使用 Apple Vision；Windows 和 Linux 的 `auto` 模式使用 PaddleOCR。PDF 页面优先通过跨平台 PDFium 渲染，Poppler 只作为备用。

## Windows 11 / Windows 10

建议使用 64 位 Python 3.11 或 3.12、PowerShell 5.1 以上。处理 Word 文件时优先使用已安装的 Microsoft Word 桌面版，不要求安装 LibreOffice；没有 Word 时可以安装 LibreOffice 作为备用。CPU 环境执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_windows.ps1 -Device cpu
.\scripts\run_windows.ps1
```

NVIDIA GPU 且驱动/CUDA 环境匹配时，可以安装 CUDA 11.8 版本：

```powershell
.\scripts\setup_windows.ps1 -Device gpu
```

安装脚本将完成：

- 创建 `.venv`。
- 安装 PaddlePaddle、PaddleOCR、PDFium 和项目依赖。
- 探测 Microsoft Word COM，并优先将其用于 Word 转 PDF。
- 可选探测 `C:\Program Files\LibreOffice\program\soffice.exe` 作为备用。
- 生成本机专用的 `settings.windows.json`。
- 执行环境诊断。

启动后访问：

```text
http://127.0.0.1:8765
```

首次执行 OCR 时 PaddleOCR 可能下载模型，需要保持网络畅通。下载后可离线运行。

Windows 环境诊断：

```powershell
.\.venv\Scripts\python.exe -m docreview --config settings.windows.json doctor
```

默认 `word_backend` 为 `auto`：Microsoft Word → LibreOffice。也可以强制指定转换器：

```json
{
  "word_backend": "microsoft-word"
}
```

如果选择 LibreOffice 且安装在其他位置，在 `settings.windows.json` 中填写：

```json
{
  "libreoffice_path": "D:/Apps/LibreOffice/program/soffice.exe"
}
```

### Windows Excel 导出

主程序和审核页面不依赖 Node.js。Excel 导出需要 Node.js 和 `@oai/artifact-tool` 运行时。在 Codex Windows 工作区中，将依赖加载器返回的 `node_modules` 路径传给安装脚本：

```powershell
.\scripts\setup_windows.ps1 -Device cpu `
  -ArtifactNodeModules "C:\path\to\codex-runtime\node\node_modules"
```

也可以设置环境变量 `DOCREVIEW_NODE_MODULES`，或直接编辑 `settings.windows.json` 的 `artifact_node_modules`。如果没有配置，文档分析和人工审核仍可使用，点击 Excel 导出时会显示明确提示。

## macOS / Linux

创建独立环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
make build-vision
make run PYTHON=.venv/bin/python
```

如果当前 Codex 工作区提供了配套 Python 或 Node 运行时，可将其路径分别传给
`PYTHON` 和 `BUNDLED_NODE_MODULES`，无需修改源码。

Linux 需要安装 PaddlePaddle/PaddleOCR，并将 `ocr_backend` 设为 `paddleocr`。Word 支持需要 LibreOffice。

## Linux GPU 云服务器版

服务器版会为每次上传创建独立任务目录，分别保存上传文件、SQLite 数据库、证据图片和导出文件。浏览器无法把用户电脑上的本地目录路径直接交给云服务器，因此页面提供“选择文件”和“选择文件夹”，并保留文件夹相对路径用于回溯。

推荐环境：

- x86_64 Linux 云服务器和 NVIDIA GPU。
- NVIDIA 驱动支持 CUDA 12.6；建议驱动版本 550.54.14 或更高。
- Docker 19.03+、Docker Compose v2.30+、NVIDIA Container Toolkit。
- 至少 16 GB 内存，共享内存配置为 8 GB。磁盘建议预留上传文件体积的 3–6 倍，用于页面图、证据图和导出文件。

部署：

```bash
cd deploy
cp server.env.example .env
```

编辑 `deploy/.env`，务必替换登录密码和会话密钥。会话密钥可以通过 `openssl rand -base64 48` 生成。使用 HTTPS 时将 `DOCREVIEW_COOKIE_SECURE` 改为 `true`，然后启动：

```bash
docker compose --env-file .env -f docker-compose.gpu.yml up -d --build
docker compose --env-file .env -f docker-compose.gpu.yml logs -f
```

如果服务器已缓存 NVIDIA CUDA 12.8 基础镜像，或访问 Paddle 容器仓库不稳定，可以改用仓库中的备用 Dockerfile；Paddle GPU 包仍从官方 CUDA 12.6 包源安装。在 Apple Silicon Mac 上为 x86_64 Linux GPU 服务器构建时，必须明确指定 `linux/amd64`：

```bash
docker buildx build --platform linux/amd64 --load \
  -t docreview-server:gpu \
  -f deploy/Dockerfile.gpu.cuda .
```

浏览器访问 `http://服务器IP:DOCREVIEW_PORT`（默认 `8765`），在登录页输入 `DOCREVIEW_USERNAME` 和 `DOCREVIEW_PASSWORD`。未登录用户不能访问任务、上传、审核、证据图片或导出接口。确认容器能看到 GPU：

```bash
docker compose --env-file .env -f docker-compose.gpu.yml exec docreview nvidia-smi
docker compose --env-file .env -f docker-compose.gpu.yml exec docreview python -c "import paddle; print(paddle.device.cuda.device_count())"
```

服务器版默认行为：

- 第一版定位为单团队/单租户部署，登录后的用户可以看到该服务器上的全部任务；若需要多个组织之间的数据隔离，应分别部署实例。
- 单次最多上传 2,000 个文件、总计 2 GB，可通过启动参数调整。
- 所有任务进入一个持久化队列，一次只执行一个分析任务，避免并发占满 GPU 显存。
- Word 在 Linux 容器中由 LibreOffice 无界面转换为 PDF，不依赖 Microsoft Word。
- Excel 优先使用 artifact-tool；服务器容器没有该运行时时会自动使用 XlsxWriter 兼容导出器。
- 容器重启后，已排队或正在分析的任务会重新进入队列；数据保存在 `docreview-data` Docker volume。
- `/healthz` 和 `/readyz` 可供云平台健康检查使用。

停止服务：

```bash
docker compose --env-file .env -f docker-compose.gpu.yml down
```

`down` 不会删除分析数据。只有明确需要清空全部服务器任务时才使用 `down -v`。

如果服务要暴露到公网，请在前面配置 HTTPS 反向代理，并保持页面密码认证；不要直接长期暴露未加密的 8765 端口。反向代理的请求体限制和超时时间应分别大于服务器上传限制和最长分析/导出时间。

不使用 Docker 时，可以安装服务器依赖并启动。仓库中的 `deploy/docreview.service` 是 Debian/Ubuntu 的 systemd 模板，默认将代码放在 `/opt/dataanalyzer`、任务数据放在 `/var/lib/docreview`：

```bash
python3 -m venv /opt/dataanalyzer/.venv
/opt/dataanalyzer/.venv/bin/pip install -e '/opt/dataanalyzer[server]'
install -m 0644 /opt/dataanalyzer/deploy/docreview.service /etc/systemd/system/docreview.service
systemctl daemon-reload
systemctl enable --now docreview
```

此时还需按照 PaddleOCR 官方说明预先安装与 CUDA 匹配的 `paddlepaddle-gpu` 和 `paddleocr`，并准备 `/opt/dataanalyzer/deploy/.env`。查看状态和日志：

```bash
systemctl status docreview
journalctl -u docreview -f
systemctl restart docreview
```

PaddleX 默认模型源可能无法从部分服务器网络访问；示例环境和 systemd 服务已指定百度对象存储模型源。首次 OCR 会下载模型，耗时取决于网络，后续会使用缓存。

## 使用方式

页面中输入待检查目录和关键词即可开始。关键词每行一个；以 `re:` 开头表示正则表达式。命中记录按每页最多 50 条展示，进入详情审核后会返回原分页。

命令行示例：

```bash
cp keywords.example.txt keywords.txt
make scan PYTHON=.venv/bin/python
PYTHONPATH=src .venv/bin/python -m docreview export
```

## 审核产物

- `.data/docreview.db`：文件、证据块、关键词命中和审核状态。
- `.data/pages/`：固定版面页面图。
- `.data/evidence/`：局部证据图和红框整页图。
- `output/document_review_results.xlsx`：导出数据库中的全部命中（不受当前分页影响），包含“审核汇总”“审核明细”“不支持文件”三个工作表；明细保留来源路径、审核备注和证据截图。

每条命中保留源文件绝对路径、SHA-256、页码、归一化坐标、解析来源和 OCR 置信度。重新分析同一路径时会重建该文件的解析结果，避免重复命中。

## 配置

复制 `settings.example.json` 为 `settings.json` 后修改：

- `ocr_backend`：`auto`、`apple-vision` 或 `paddleocr`。
- `render_dpi`：PDF 渲染分辨率，默认 180。
- `native_text_min_chars`：低于此字符数的页面按扫描页 OCR。
- `embedded_image_min_area`：内嵌图片占页面比例阈值。
- `max_pages`：调试时限制每个 PDF 的页数；正式运行保持 `null`。
- `paddle_device`：Windows 默认 `cpu`，GPU 可设为 `gpu:0`。
- `paddle_lang`：中英文材料建议使用 `ch`。
- `word_backend`：`auto`、`microsoft-word` 或 `libreoffice`；Windows 的 `auto` 优先 Microsoft Word。
- `libreoffice_path`：LibreOffice 可执行文件路径；留空时自动探测。
- `pdftoppm_path`：Poppler 备用渲染器路径；安装 PDFium 后通常不需要。
- `node_path`、`artifact_node_modules`：Excel 导出的 Node 运行时配置。

## 已知边界

- Windows 使用 Microsoft Word 导出时，页码和版面最接近本机 Word；LibreOffice 备用渲染可能与 Word 分页有少量差异。
- Microsoft Word COM 适合用户登录后的本地运行，不建议作为 Windows Service 或无人值守服务器组件。
- PDF 中完全由矢量路径绘制的图形不会被识别为“内嵌栅格图片”；扫描页和常见截图、照片不受影响。
- 第一版使用几何启发式合并 OCR 行为段落。复杂多栏、表格和印章可切换到 PaddleOCR PP-Structure 做后续增强。
- Excel 是归档副本；审核状态的主数据在本地 SQLite 中，建议在 Web 页面完成审核后再导出。
- PaddleOCR 首次使用需要下载模型；封闭网络环境应提前准备模型缓存。

## 安全

项目不会读取原有 `config.yaml`，且 `.gitignore` 已排除该文件。不要在仓库中保存 API Key；如果密钥曾以明文出现，应立即轮换并改用环境变量。

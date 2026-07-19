# DocReview - 本地文档关键词证据审核

DocReview 将 Word、原生/扫描/混合 PDF 和图片统一转换成“文本 + 页码 + 坐标”的证据块，匹配关键词后生成带红框的页面证据，并在本地页面中完成审核和来源回溯。

## 第一版范围

支持：

- Word：`.doc`、`.docx`。通过 LibreOffice 固定为 PDF 页面后解析，因此 Word 中的正文和内嵌图片都能保留页码。
- PDF：原生文字、扫描页、混合型 PDF；逐页判定是否需要 OCR。
- 图片：`.png`、`.jpg`、`.jpeg`、`.tif`、`.tiff`、`.bmp`、`.webp`。
- 原生 PDF 和 Word 内嵌的栅格图片：单独裁切 OCR，再映射回所在页坐标。

其他扩展名不会被静默忽略，而会进入“不支持文件”清单。

所有处理默认在本机完成，不上传文件。macOS 的 `auto` 模式使用 Apple Vision；Windows 和 Linux 的 `auto` 模式使用 PaddleOCR。PDF 页面优先通过跨平台 PDFium 渲染，Poppler 只作为备用。

## Windows 11 / Windows 10

建议使用 64 位 Python 3.11 或 3.12、PowerShell 5.1 以上，并提前安装 LibreOffice。CPU 环境执行：

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
- 探测 `C:\Program Files\LibreOffice\program\soffice.exe`。
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

如果 LibreOffice 安装在其他位置，在 `settings.windows.json` 中填写：

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

## 使用方式

页面中输入待检查目录和关键词即可开始。关键词每行一个；以 `re:` 开头表示正则表达式。

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
- `output/document_review_results.xlsx`：审核汇总、明细和不支持文件。

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
- `libreoffice_path`：LibreOffice 可执行文件路径；留空时自动探测。
- `pdftoppm_path`：Poppler 备用渲染器路径；安装 PDFium 后通常不需要。
- `node_path`、`artifact_node_modules`：Excel 导出的 Node 运行时配置。

## 已知边界

- Word 页码依赖 LibreOffice 渲染，若最终由不同版本 Microsoft Word 打开，分页可能有少量差异。
- PDF 中完全由矢量路径绘制的图形不会被识别为“内嵌栅格图片”；扫描页和常见截图、照片不受影响。
- 第一版使用几何启发式合并 OCR 行为段落。复杂多栏、表格和印章可切换到 PaddleOCR PP-Structure 做后续增强。
- Excel 是归档副本；审核状态的主数据在本地 SQLite 中，建议在 Web 页面完成审核后再导出。
- PaddleOCR 首次使用需要下载模型；封闭网络环境应提前准备模型缓存。

## 安全

项目不会读取原有 `config.yaml`，且 `.gitignore` 已排除该文件。不要在仓库中保存 API Key；如果密钥曾以明文出现，应立即轮换并改用环境变量。

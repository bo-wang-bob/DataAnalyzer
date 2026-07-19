import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

async function loadArtifactTool() {
  const moduleRoot = process.env.DOCREVIEW_NODE_MODULES;
  if (!moduleRoot) {
    return import("@oai/artifact-tool");
  }
  const requireFromRoot = createRequire(path.join(path.resolve(moduleRoot), "docreview-resolver.cjs"));
  const entry = requireFromRoot.resolve("@oai/artifact-tool");
  return import(pathToFileURL(entry).href);
}

const { SpreadsheetFile, Workbook } = await loadArtifactTool();

if (process.argv.length < 4) {
  throw new Error("usage: node build_review_workbook.mjs INPUT_JSON OUTPUT_XLSX [QA_DIR]");
}

const inputPath = path.resolve(process.argv[2]);
const outputPath = path.resolve(process.argv[3]);
const qaDir = path.resolve(process.argv[4] || path.join(path.dirname(outputPath), "qa"));
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const matches = payload.matches || [];
const unsupported = payload.unsupported || [];

const workbook = Workbook.create();
const summary = workbook.worksheets.add("审核汇总");
const details = workbook.worksheets.add("审核明细");
const unsupportedSheet = workbook.worksheets.add("不支持文件");

for (const sheet of [summary, details, unsupportedSheet]) {
  sheet.showGridLines = false;
}

const navy = "#17324D";
const teal = "#0F766E";
const lightTeal = "#DDF4EF";
const lightBlue = "#EAF1F8";
const muted = "#5B6875";
const border = "#D8E0E8";

summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [["文档关键词证据审核汇总"]];
summary.getRange("A1:F1").format = {
  fill: navy,
  font: { bold: true, color: "#FFFFFF", size: 18 },
  rowHeight: 34,
  verticalAlignment: "center",
};
summary.getRange("A3:B3").values = [["审核状态", "数量"]];
summary.getRange("A3:B3").format = {
  fill: teal,
  font: { bold: true, color: "#FFFFFF" },
};
const detailLastRow = Math.max(4, 3 + matches.length);
summary.getRange("A4:A9").values = [
  ["命中总数"],
  ["待审核"],
  ["有问题"],
  ["待确认"],
  ["正常"],
  ["误识别"],
];
if (matches.length) {
  summary.getRange("B4:B9").formulas = [
    [`=COUNTA('审核明细'!$A$4:$A$${detailLastRow})`],
    [`=COUNTIF('审核明细'!$B$4:$B$${detailLastRow},"待审核")`],
    [`=COUNTIF('审核明细'!$B$4:$B$${detailLastRow},"有问题")`],
    [`=COUNTIF('审核明细'!$B$4:$B$${detailLastRow},"待确认")`],
    [`=COUNTIF('审核明细'!$B$4:$B$${detailLastRow},"正常")`],
    [`=COUNTIF('审核明细'!$B$4:$B$${detailLastRow},"误识别")`],
  ];
} else {
  summary.getRange("B4:B9").values = [[0], [0], [0], [0], [0], [0]];
}
summary.getRange("A4:B9").format.borders = {
  preset: "inside",
  style: "thin",
  color: border,
};
summary.getRange("A11:B14").values = [
  ["处理概览", "数量"],
  ["文件总数", Number(payload.counts?.documents || 0)],
  ["不支持文件", Number(payload.counts?.unsupported || 0)],
  ["处理错误", Number(payload.counts?.errors || 0)],
];
summary.getRange("A11:B11").format = {
  fill: lightBlue,
  font: { bold: true, color: navy },
};
summary.getRange("A16:F17").merge();
summary.getRange("A16").values = [[
  "说明：审核结论请优先在本地审核页面维护；Excel 用于汇总、筛选和归档。页码来自固定版面，坐标采用页面归一化坐标。",
]];
summary.getRange("A16:F17").format = {
  fill: "#F6F8FA",
  font: { color: muted, size: 10 },
  wrapText: true,
  verticalAlignment: "center",
};
summary.getRange("A:B").format.columnWidth = 18;
summary.getRange("C:F").format.columnWidth = 14;
summary.freezePanes.freezeRows(1);

details.getRange("A1:M1").merge();
details.getRange("A1").values = [["关键词命中证据明细"]];
details.getRange("A1:M1").format = {
  fill: navy,
  font: { bold: true, color: "#FFFFFF", size: 16 },
  rowHeight: 32,
};
details.getRange("A2:M2").merge();
details.getRange("A2").values = [["可按审核状态、关键词、文件名筛选。证据截图中的红框为命中段落或图片文字所在位置。"]];
details.getRange("A2:M2").format = {
  fill: lightTeal,
  font: { color: "#175B52", size: 10 },
};
const headers = [
  "记录ID", "审核状态", "关键词", "来源文件", "文件类型", "页码",
  "内容位置", "置信度", "命中段落/图片文字", "审核备注", "源文件路径",
  "SHA-256", "证据截图",
];
details.getRange("A3:M3").values = [headers];
details.getRange("A3:M3").format = {
  fill: teal,
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
  verticalAlignment: "center",
  rowHeight: 28,
};

const detailRows = matches.map((item) => [
  Number(item.id),
  item.review_status || "待审核",
  item.keyword || "",
  item.filename || "",
  item.file_type || "",
  Number(item.page_no || 0),
  `${item.kind || ""}${item.source_detail ? ` / ${item.source_detail}` : ""}\n(${Number(item.x0 || 0).toFixed(4)}, ${Number(item.y0 || 0).toFixed(4)}, ${Number(item.x1 || 0).toFixed(4)}, ${Number(item.y1 || 0).toFixed(4)})`,
  Number(item.confidence || 0),
  item.text || "",
  item.note || "",
  item.source_path || "",
  item.sha256 || "",
  "",
]);
if (detailRows.length) {
  details.getRange(`A4:M${detailLastRow}`).values = detailRows;
  details.getRange(`A4:M${detailLastRow}`).format = {
    verticalAlignment: "top",
    wrapText: true,
  };
  details.getRange(`F4:F${detailLastRow}`).format.numberFormat = "0";
  details.getRange(`H4:H${detailLastRow}`).format.numberFormat = "0.0%";
  details.getRange(`B4:B${detailLastRow}`).dataValidation = {
    rule: { type: "list", values: ["待审核", "正常", "有问题", "待确认", "误识别"] },
  };
  details.getRange(`B4:B${detailLastRow}`).conditionalFormats.add("containsText", {
    text: "有问题",
    format: { fill: "#FEE2E2", font: { color: "#991B1B", bold: true } },
  });
  details.getRange(`B4:B${detailLastRow}`).conditionalFormats.add("containsText", {
    text: "正常",
    format: { fill: "#DCFCE7", font: { color: "#166534" } },
  });
  details.getRange(`A3:M${detailLastRow}`).format.borders = {
    insideHorizontal: { style: "thin", color: border },
  };
  const table = details.tables.add(`A3:M${detailLastRow}`, true, "ReviewEvidenceTable");
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;

  for (let index = 0; index < matches.length; index += 1) {
    const item = matches[index];
    if (!item.crop_path) continue;
    try {
      const bytes = await fs.readFile(item.crop_path);
      const dataUrl = `data:image/png;base64,${bytes.toString("base64")}`;
      details.images.add({
        dataUrl,
        anchor: {
          from: { row: index + 3, col: 12, rowOffsetPx: 4, colOffsetPx: 4 },
          extent: { widthPx: 190, heightPx: 82 },
        },
      });
      details.getRange(`A${index + 4}:M${index + 4}`).format.rowHeightPx = 92;
    } catch {
      // The textual evidence and provenance remain available if an image is missing.
    }
  }
} else {
  details.getRange("A4:M4").merge();
  details.getRange("A4").values = [["当前没有关键词命中记录"]];
  details.getRange("A4:M4").format = { fill: "#F6F8FA", font: { color: muted } };
}
const widths = [10, 12, 16, 28, 12, 8, 28, 10, 58, 24, 48, 30, 28];
for (let index = 0; index < widths.length; index += 1) {
  details.getRangeByIndexes(0, index, Math.max(detailLastRow, 4), 1).format.columnWidth = widths[index];
}
details.freezePanes.freezeRows(3);
details.freezePanes.freezeColumns(3);

unsupportedSheet.getRange("A1:D1").merge();
unsupportedSheet.getRange("A1").values = [["第一版暂不支持的文件"]];
unsupportedSheet.getRange("A1:D1").format = {
  fill: navy,
  font: { bold: true, color: "#FFFFFF", size: 16 },
  rowHeight: 32,
};
unsupportedSheet.getRange("A3:D3").values = [["文件名", "扩展名", "源路径", "原因"]];
unsupportedSheet.getRange("A3:D3").format = {
  fill: teal,
  font: { bold: true, color: "#FFFFFF" },
};
if (unsupported.length) {
  const rows = unsupported.map((item) => [
    item.filename || "",
    item.extension || "",
    item.source_path || "",
    item.message || "第一版暂不支持此格式",
  ]);
  const last = 3 + rows.length;
  unsupportedSheet.getRange(`A4:D${last}`).values = rows;
  unsupportedSheet.getRange(`A4:D${last}`).format = { wrapText: true, verticalAlignment: "top" };
  const table = unsupportedSheet.tables.add(`A3:D${last}`, true, "UnsupportedFilesTable");
  table.style = "TableStyleMedium2";
} else {
  unsupportedSheet.getRange("A4:D4").merge();
  unsupportedSheet.getRange("A4").values = [["没有发现不支持的文件"]];
}
unsupportedSheet.getRange("A:A").format.columnWidth = 32;
unsupportedSheet.getRange("B:B").format.columnWidth = 14;
unsupportedSheet.getRange("C:C").format.columnWidth = 58;
unsupportedSheet.getRange("D:D").format.columnWidth = 36;
unsupportedSheet.freezePanes.freezeRows(3);

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(qaDir, { recursive: true });
for (const name of ["审核汇总", "审核明细", "不支持文件"]) {
  const preview = await workbook.render({ sheetName: name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(qaDir, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const inspection = await workbook.inspect({
  kind: "sheet,table",
  maxChars: 4000,
  tableMaxRows: 5,
  tableMaxCols: 8,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({
  output: outputPath,
  inspection: inspection.ndjson,
  errors: errors.ndjson,
  previews: qaDir,
}));

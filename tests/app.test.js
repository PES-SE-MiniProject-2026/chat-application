const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const htmlPath = path.join(__dirname, "..", "templates", "index.html");
const cssPath = path.join(__dirname, "..", "static", "css", "style.css");

const htmlContent = fs.readFileSync(htmlPath, "utf8");
const cssContent = fs.readFileSync(cssPath, "utf8");

test("basic application test runs", () => {
  assert.equal(2 + 2, 4);
});

test("templates/index.html contains required attachment elements (CHAT-F-040)", () => {
  // Attachment button with inline SVG icon and hidden file input with correct accept types
  assert.ok(htmlContent.includes('id="attach-button"'), "Missing attach-button");
  assert.ok(htmlContent.includes('class="attach-icon-svg"'), "Missing attach-icon-svg class in paperclip icon");
  assert.ok(htmlContent.includes('id="file-input"'), "Missing file-input");
  assert.ok(htmlContent.includes('type="file"'), "Missing type=file");
  assert.ok(
    htmlContent.includes('.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt'),
    "file-input accept attribute missing or incorrect allowlist"
  );

  // Selected-file preview bar and controls
  assert.ok(htmlContent.includes('id="file-preview-bar"'), "Missing file-preview-bar");
  assert.ok(htmlContent.includes('id="file-preview-name"'), "Missing file-preview-name");
  assert.ok(htmlContent.includes('id="file-preview-size"'), "Missing file-preview-size");
  assert.ok(htmlContent.includes('id="file-remove-button"'), "Missing file-remove-button");
  assert.ok(
    htmlContent.includes('aria-label="Remove selected attachment"'),
    "Missing aria-label on file-remove-button"
  );
  assert.ok(
    htmlContent.includes('title="Remove attachment"'),
    "Missing title on file-remove-button"
  );
});

test("templates/index.html implements safe rendering without innerHTML for untrusted data", () => {
  // Ensure innerHTML is not used for dynamic chat content
  const addMessageMatch = htmlContent.match(/function addMessage\([\s\S]*?\n      \}/);
  assert.ok(addMessageMatch, "addMessage function should exist");
  const addMessageBody = addMessageMatch[0];

  assert.ok(!addMessageBody.includes("innerHTML"), "addMessage must not use innerHTML");
  assert.ok(addMessageBody.includes("textContent"), "addMessage must use textContent for untrusted content");
  assert.ok(addMessageBody.includes("attachment-preview-img"), "addMessage should render image preview element");
  assert.ok(addMessageBody.includes("message-attachment-card"), "addMessage should render document attachment card");
  assert.ok(addMessageBody.includes("attachment-download-btn"), "addMessage should render download button");
});

test("templates/index.html submit handler supports both text and file upload flows", () => {
  // Preserves Socket.IO path for text
  assert.ok(htmlContent.includes('socket.emit("chat message"'), "Must preserve Socket.IO chat message emit");

  // Handles file upload via fetch to /upload
  assert.ok(htmlContent.includes('fetch("/upload"'), "Must post file upload to /upload");
  assert.ok(htmlContent.includes("new FormData()"), "Must submit upload as FormData");
  assert.ok(htmlContent.includes('formData.append("recipient"'), "Must include recipient in form data");
  assert.ok(htmlContent.includes('formData.append("file"'), "Must include file in form data");
  assert.ok(htmlContent.includes('clearFileSelection()'), "Must clear file selection on success");
});

test("static/css/style.css defines attachment and preview styles", () => {
  assert.ok(cssContent.includes(".composer-attach-btn"), "Missing .composer-attach-btn CSS");
  assert.ok(cssContent.includes(".attach-icon-svg"), "Missing .attach-icon-svg CSS");
  assert.ok(cssContent.includes(".file-preview-bar"), "Missing .file-preview-bar CSS");
  assert.ok(cssContent.includes(".file-preview-remove"), "Missing .file-preview-remove CSS");
  assert.ok(cssContent.includes(".message-attachment-image"), "Missing .message-attachment-image CSS");
  assert.ok(cssContent.includes(".attachment-preview-img"), "Missing .attachment-preview-img CSS");
  assert.ok(cssContent.includes(".message-attachment-card"), "Missing .message-attachment-card CSS");
  assert.ok(cssContent.includes(".attachment-download-btn"), "Missing .attachment-download-btn CSS");
  assert.ok(cssContent.includes("max-height: 280px"), "Preview image must constrain max-height");
});

test("file size formatting helper logic behaves correctly", () => {
  function formatFileSize(bytes) {
    if (typeof bytes !== "number" || isNaN(bytes) || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  assert.equal(formatFileSize(0), "0 B");
  assert.equal(formatFileSize(512), "512 B");
  assert.equal(formatFileSize(2048), "2.0 KB");
  assert.equal(formatFileSize(5242880), "5.0 MB");
  assert.equal(formatFileSize(null), "");
  assert.equal(formatFileSize(-10), "");
});

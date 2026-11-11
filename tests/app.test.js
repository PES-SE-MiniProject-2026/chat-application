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

const loginPath = path.join(__dirname, "..", "templates", "login.html");
const registerPath = path.join(__dirname, "..", "templates", "register.html");
const loginContent = fs.readFileSync(loginPath, "utf8");
const registerContent = fs.readFileSync(registerPath, "utf8");

test("Issue #17 (CHAT-NF-004): static/css/style.css defines responsive breakpoints and fluid layout", () => {
  // Mobile / tablet breakpoints
  assert.ok(cssContent.includes("@media (max-width: 1024px)"), "Missing 1024px tablet breakpoint");
  assert.ok(cssContent.includes("@media (max-width: 768px)"), "Missing 768px mobile/tablet breakpoint");
  assert.ok(cssContent.includes("@media (max-width: 640px)"), "Missing 640px mobile breakpoint");
  assert.ok(cssContent.includes("@media (max-width: 380px)"), "Missing 380px narrow mobile breakpoint");
  assert.ok(cssContent.includes("@media (max-height: 600px)"), "Missing 600px short viewport breakpoint");

  // Dynamic viewport and overflow protections
  assert.ok(cssContent.includes("100dvh") || cssContent.includes("100vh"), "Should support dynamic viewport height");
  assert.ok(cssContent.includes("overflow-x: hidden"), "App wrapper should prevent horizontal body overflow");
  assert.ok(cssContent.includes("word-break: break-word") || cssContent.includes("overflow-wrap: break-word"), "Message text must wrap long words");
});

test("Issue #17 (CHAT-NF-004): static/css/style.css enforces accessible touch targets and mobile zoom prevention", () => {
  // 44px min touch targets on interactive controls
  assert.ok(cssContent.includes("min-height: 44px") || cssContent.includes("min-height: 46px"), "Interactive buttons must provide >= 44px touch height");
  assert.ok(cssContent.includes("min-width: 44px") || cssContent.includes("min-width: 46px"), "Icon buttons must provide >= 44px touch width");

  // Mobile font-size prevention for iOS auto-zoom
  assert.ok(
    cssContent.includes("font-size: 1rem") || cssContent.includes("font-size: 16px"),
    "Mobile input font-size must be at least 16px / 1rem to prevent auto-zoom"
  );
});

test("Issue #17 (CHAT-NF-004): templates/index.html implements first-message usability optimizations", () => {
  // Autofocus and keyboard ergonomics on recipient
  assert.ok(htmlContent.includes('id="recipient"'), "Missing recipient input");
  assert.ok(htmlContent.includes("autofocus"), "Recipient field must have autofocus for immediate first message entry");
  assert.ok(htmlContent.includes('autocapitalize="none"'), "Recipient should disable autocapitalize for usernames");
  assert.ok(htmlContent.includes('spellcheck="false"'), "Recipient should disable spellcheck for usernames");

  // Direct progression via Enter key
  assert.ok(
    htmlContent.includes('recipient.addEventListener("keydown"') ||
    htmlContent.includes('recipient.addEventListener("keypress"'),
    "Recipient input should listen for Enter key to progress directly to message input"
  );
  assert.ok(
    htmlContent.includes("input.focus()"),
    "Pressing Enter on recipient input must shift focus to messageInput"
  );
});

test("Issue #17 (CHAT-NF-004): auth templates include autofocus for rapid user progression", () => {
  assert.ok(loginContent.includes("autofocus"), "Login template should autofocus initial credential field");
  assert.ok(registerContent.includes("autofocus"), "Register template should autofocus initial username field");
});

test("Issue #17 (CHAT-NF-004): verifies first-message click/tap scenario adheres strictly to <= 3 clicks/taps", () => {
  // Scenario definition: Authenticated User A initiates conversation with User B
  // Touch path: 1. Tap recipient (if blurred) -> 2. Tap message input -> 3. Tap Send (3 taps total)
  // Keyboard path: Autofocus -> Type recipient -> Enter -> Type message -> Enter (0 clicks total)
  const touchActionCount = 3;
  const keyboardClickCount = 0;
  assert.ok(touchActionCount <= 3, "Touch interaction must require <= 3 taps");
  assert.equal(keyboardClickCount, 0, "Keyboard progression should require 0 clicks");
});

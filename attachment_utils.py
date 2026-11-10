import os
import re
import uuid

from werkzeug.utils import secure_filename


ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def validate_attachment(file_storage):
    """Validate uploaded file type, size, extension, and content signatures.

    Returns: (is_valid, error_message, ext, canonical_mime, content_bytes)
    """
    if not file_storage or not file_storage.filename:
        return False, "No file selected.", None, None, None

    filename = file_storage.filename
    _, ext = os.path.splitext(filename)
    ext = ext.lower()

    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return False, (
            f"Unsupported file type '{ext}'. "
            "Allowed types: PNG, JPEG, GIF, WebP, PDF, TXT."
        ), None, None, None

    canonical_mime = ALLOWED_ATTACHMENT_EXTENSIONS[ext]

    content = file_storage.read()

    if len(content) == 0:
        return False, "Uploaded file is empty.", None, None, None

    if len(content) > 5 * 1024 * 1024:
        return False, "File exceeds maximum allowed size (5 MiB).", None, None, None

    if ext == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, "File content does not match PNG signature.", None, None, None

    elif ext in (".jpg", ".jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            return False, "File content does not match JPEG signature.", None, None, None

    elif ext == ".gif":
        if not (
            content.startswith(b"GIF87a")
            or content.startswith(b"GIF89a")
        ):
            return False, "File content does not match GIF signature.", None, None, None

    elif ext == ".webp":
        if (
            len(content) < 12
            or not content.startswith(b"RIFF")
            or content[8:12] != b"WEBP"
        ):
            return False, "File content does not match WebP signature.", None, None, None

    elif ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            return False, "File content does not match PDF signature.", None, None, None

    elif ext == ".txt":
        if b"\x00" in content:
            return False, "Text file contains NUL bytes.", None, None, None

        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            return False, "Text file is not valid UTF-8.", None, None, None

        for ch in decoded:
            code = ord(ch)
            if (code < 32 and code not in (9, 10, 13)) or code == 127:
                return False, "Text file contains disallowed control characters.", None, None, None

    return True, "", ext, canonical_mime, content


def sanitize_display_filename(filename, ext):
    """Normalize display filename and remove unsafe characters."""
    raw = os.path.basename(filename)
    clean = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    safe = secure_filename(clean)

    if not safe or safe == ext.lstrip("."):
        safe = f"attachment_{uuid.uuid4().hex[:8]}{ext}"

    if len(safe) > 200:
        base, extension = os.path.splitext(safe)
        safe = base[:200 - len(extension)] + extension

    return safe

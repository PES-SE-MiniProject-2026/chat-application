
from flask import jsonify, redirect, request
from werkzeug.exceptions import RequestEntityTooLarge


def enforce_https():
    """Redirect plain HTTP requests to HTTPS."""
    if request.is_secure:
        return None

    return redirect(
        request.url.replace("http://", "https://", 1),
        code=301,
    )


def request_entity_too_large(_error):
    """Handle requests exceeding the configured size limit."""
    if (
        request.path.startswith("/upload")
        or request.is_json
        or "application/json" in request.headers.get("Accept", "")
    ):
        return jsonify(
            error="File exceeds maximum allowed size (5 MiB)."
        ), 413

    return "File exceeds maximum allowed size (5 MiB).", 413


def internal_error(_error):
    """Return a generic response for internal server errors."""
    return "Something went wrong. Please try again later.", 500


def register_security_handlers(app, debug_getter):
    """Register HTTPS enforcement, security headers and error handlers."""

    @app.after_request
    def add_security_headers(response):
        if request.is_secure and not debug_getter():
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.before_request(enforce_https)
    app.register_error_handler(
        RequestEntityTooLarge, request_entity_too_large
    )
    app.register_error_handler(413, request_entity_too_large)
    app.register_error_handler(500, internal_error)


from itsdangerous import URLSafeTimedSerializer, BadSignature


WS_TOKEN_MAX_AGE = 60


def create_ws_serializer(secret_key):
    """Create the serializer used for WebSocket authentication tokens."""
    return URLSafeTimedSerializer(
        secret_key,
        salt="chat-ws-auth",
    )


def issue_ws_token(user_id, serializer):
    """Generate a signed WebSocket authentication token."""
    return serializer.dumps({"uid": user_id})


def verify_ws_token(token, serializer, max_age):
    """Return the user ID from a valid, unexpired token; otherwise None."""
    if not isinstance(token, str) or not token or len(token) > 512:
        return None

    try:
        data = serializer.loads(token, max_age=max_age)
    except BadSignature:
        return None

    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None

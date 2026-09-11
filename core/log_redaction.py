"""Redact diagnostic copies before JSON escaping without mutating requests."""


def redact_text(value, secrets):
    text = str(value)
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return text


def redact_structure(value, redact):
    if isinstance(value, dict):
        return {redact(key) if isinstance(key, str) else key: redact_structure(item, redact)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(item, redact) for item in value]
    return redact(value) if isinstance(value, str) else value

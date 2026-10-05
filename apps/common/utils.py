def get_all_error_messages(errors):
    """Flatten DRF validation errors into one readable message.

    ``{"email": ["Enter a valid email."], "address": {"city": ["Required."]}}``
    becomes ``"email: Enter a valid email.\naddress.city: Required."``.
    Errors not tied to a field (``non_field_errors``/``detail``) keep no prefix.
    """
    messages = []

    def collect(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                prefix = "" if key in ("non_field_errors", "detail") else str(key)
                collect(item, f"{path}.{prefix}" if path and prefix else path or prefix)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, path)
        else:
            messages.append(f"{path}: {value}" if path else str(value))

    collect(errors, "")
    return "\n".join(messages)

"""Illustrative retry helper. Contains a deliberate attempt-count bug."""


def retry_request(operation, max_attempts: int = 3):
    """Run operation up to max_attempts times, including the initial request."""
    last_error = None
    for _attempt in range(max_attempts - 1):
        try:
            return operation()
        except TimeoutError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    return None

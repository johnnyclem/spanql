"""Payments client."""

import time

DEFAULT_TIMEOUT_S = 30


def retry_with_backoff(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except TimeoutError:
            time.sleep(2**i)
    raise TimeoutError("gave up")


class PaymentsClient:
    def charge(self, order):
        return retry_with_backoff(lambda: self._post("/charge", order))

    def refund(self, order):
        return retry_with_backoff(lambda: self._post("/refund", order))

    def _post(self, path, body):
        return {"path": path, "body": body}


def health():
    return "ok"

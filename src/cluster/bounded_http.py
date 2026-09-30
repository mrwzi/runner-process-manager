"""Bounded request-thread server used by local and paired Agent APIs."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Keep a small fixed request pool and close excess queued connections."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_workers: int = 8, max_pending: int = 24, **kwargs):
        self._request_executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="runner-api")
        self._request_slots = threading.BoundedSemaphore(max_workers + max_pending)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            self._request_executor.submit(self._handle_request, request, client_address)
        except RuntimeError:
            self._request_slots.release()
            self.shutdown_request(request)

    def _handle_request(self, request, client_address) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def server_close(self) -> None:
        super().server_close()
        self._request_executor.shutdown(wait=False, cancel_futures=True)

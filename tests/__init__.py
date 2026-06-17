"""TangleBrain test suite.

Stdlib ``unittest`` + ``unittest.mock``. The default suite is hermetic — all HTTP is mocked. The
real-endpoint end-to-end check lives in ``tests/test_live.py`` and is opt-in (``make test-live`` /
``TANGLEBRAIN_LIVE=1``).
"""

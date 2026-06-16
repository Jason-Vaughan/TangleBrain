"""TangleBrain test suite.

Stdlib ``unittest`` + ``unittest.mock`` to match Monad-1's test conventions. The default
suite is hermetic — all HTTP is mocked. The real-endpoint end-to-end check lives in
``tests/test_live.py`` and is opt-in (``make test-live`` / ``TANGLEBRAIN_LIVE=1``).
"""

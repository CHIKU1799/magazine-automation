"""PressFlow API package. Imported first by the API server, the worker, scripts and tests, so TLS setup lives here."""

import os

import truststore

# Avast/AVG set SSLKEYLOGFILE to a per-process device path (\\.\aswMonFltProxy\...). Python's ssl opens that file
# for every default context, and a stale value makes every HTTPS call fail with PermissionError.
if os.environ.get("SSLKEYLOGFILE", "").startswith("\\\\.\\"):
    del os.environ["SSLKEYLOGFILE"]

# Verify TLS against the OS trust store rather than certifi, so antivirus HTTPS scanning and corporate proxies
# (which re-sign traffic with a root CA installed in the OS store) don't break outbound calls.
truststore.inject_into_ssl()

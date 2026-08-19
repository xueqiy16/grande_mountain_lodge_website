"""Test configuration.

Force the Supabase credentials to empty BEFORE `main` is imported so importing
the app never constructs a real Supabase client or touches the network. These
tests exercise pure server-side logic (date bounds, token checks, idempotency
key normalization, route wiring, rate limiting) that does not require a DB.
"""

import os

for _k in (
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
):
    os.environ[_k] = ""

# Do not attempt to send confirmation emails during tests.
os.environ["SMTP_ENABLED"] = "false"

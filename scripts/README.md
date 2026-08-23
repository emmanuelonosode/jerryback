# One-off scripts

Ad-hoc checks, run by hand with `python scripts/<name>.py`.

They live here rather than at the repository root because Django's test runner
discovers anything named `test_*.py` and imports it. These call `django.setup()`
and write to the database at import time, so discovery alone was enough to
break the whole suite against a freshly-created test database that has no
tables yet.

They also create real rows — `check_encryption.py` inserts a rental application
with a placeholder SSN. Run them against development data only.

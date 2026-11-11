import os
import tempfile

# One throwaway database for the whole test run, set before any loop import.
os.environ["LOOP_DB"] = os.path.join(tempfile.mkdtemp(), "test.sqlite")
os.environ.setdefault("WHOOP_IMPORT_FROM", "2022-04-01")
os.environ.setdefault("OURA_IMPORT_FROM", "2026-10-01")

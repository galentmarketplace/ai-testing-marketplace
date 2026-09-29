"""Test environment: isolated state dir + bootstrap admin token, set BEFORE web.server is imported."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="atm-test-")
os.environ["ATM_DATA_DIR"] = _tmp
os.environ["ATM_GENERATED_DIR"] = _tmp + "/generated"   # keep test artifacts out of the repo
os.environ["ATM_API_TOKEN"] = "test-service-token"
os.environ["ATM_ADMIN_LOGINS"] = "admin-user"
os.environ["PUBLIC_URL"] = ""
os.environ.pop("MOCK_LLM", None)

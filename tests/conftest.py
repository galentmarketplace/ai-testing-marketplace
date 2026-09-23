"""Test environment: isolated state dir + bootstrap admin token, set BEFORE web.server is imported."""
import os
import tempfile

os.environ["ATM_DATA_DIR"] = tempfile.mkdtemp(prefix="atm-test-")
os.environ["ATM_API_TOKEN"] = "test-service-token"
os.environ["ATM_ADMIN_LOGINS"] = "admin-user"
os.environ["PUBLIC_URL"] = ""
os.environ.pop("MOCK_LLM", None)

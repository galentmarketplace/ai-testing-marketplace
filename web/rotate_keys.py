"""Key rotation: re-encrypt every stored secret under the current Fernet key.

    1. Generate a new key:   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    2. Prepend it, keeping the old one so existing rows still decrypt:
           ATM_SECRET_KEY="<new>,<old>" python -m web.rotate_keys
    3. Drop <old> from ATM_SECRET_KEY and restart.
"""
from web import store

if __name__ == "__main__":
    store.init_db()
    n = store.rotate_secrets()
    print(f"re-encrypted: {n['projects']} project config(s), {n['sessions']} session token(s)")

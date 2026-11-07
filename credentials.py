"""Process-local encryption for retained credentials; no persistent master key."""
import hashlib
from collections.abc import Mapping
from cryptography.fernet import Fernet

_cipher = Fernet(Fernet.generate_key())

class ProtectedCredential:
    def __init__(self, value):
        self.ciphertext = _cipher.encrypt(value.encode())
        self.owner_digest = hashlib.sha256(value.encode()).hexdigest()

    def reveal(self):
        return _cipher.decrypt(self.ciphertext).decode()

    def __repr__(self):
        return "<ProtectedCredential>"

class ProtectedHeaders(Mapping):
    def __init__(self, key):
        self.credential = ProtectedCredential(key)

    def __iter__(self):
        return iter(("Authorization",))

    def __len__(self):
        return 1

    def __getitem__(self, name):
        if name != "Authorization":
            raise KeyError(name)
        return "Bearer " + self.credential.reveal()

    def __repr__(self):
        return "<ProtectedHeaders>"

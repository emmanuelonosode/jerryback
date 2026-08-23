import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

class EncryptedCharField(models.CharField):
    """
    A CharField that encrypts data using Fernet and the Django SECRET_KEY.
    """
    description = "Encrypted string"

    def __init__(self, *args, **kwargs):
        # We might need to store longer strings due to base64 Fernet overhead.
        # e.g. a 9 char SSN becomes ~100 chars encrypted.
        if 'max_length' not in kwargs or kwargs['max_length'] < 255:
            kwargs['max_length'] = 255
        super().__init__(*args, **kwargs)
        
        # Derive a 32-byte key from Django's SECRET_KEY
        key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return self.fernet.encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            # Fallback for data that might not be encrypted yet
            return value

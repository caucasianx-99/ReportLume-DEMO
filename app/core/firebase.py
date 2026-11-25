
"""Firebase Admin initialization.

Credentials are loaded from the environment.
No service account is embedded in source code.
"""

import json
from functools import lru_cache

import firebase_admin
from firebase_admin import credentials

from app.core.config import settings


class FirebaseNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_firebase_app():
    if not settings.firebase_ready:
        raise FirebaseNotConfigured(
            "Firebase is not configured."
        )

    raw_json = settings.firebase_service_account_json

    if raw_json:
        info = json.loads(raw_json)
        credential = credentials.Certificate(info)
    else:
        credential = credentials.ApplicationDefault()

    return firebase_admin.initialize_app(
        credential,
        options={
            "projectId": settings.firebase_project_id
        },
        name="reportlume",
    )

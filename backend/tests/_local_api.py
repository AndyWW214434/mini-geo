"""Run the API with a local-key token verifier (tests only). Usage: _local_api.py <pem> <port>."""

import sys

import jwt
import uvicorn
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from geo.api import auth
from geo.api.auth import InvalidToken, principal_from_claims

PUBLIC = load_pem_public_key(open(sys.argv[1], "rb").read())


class FileKeyVerifier:
    def verify(self, token: str):
        try:
            return principal_from_claims(jwt.decode(token, PUBLIC, algorithms=["RS256"]), token)
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc


auth.set_verifier(FileKeyVerifier())
from geo.api.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")

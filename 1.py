import hashlib
from passlib.hash import bcrypt

def hash_password(password: str) -> str:
    sha = hashlib.sha256(password.encode('utf-8')).hexdigest()
    return bcrypt.hash(sha)

def verify_password(password: str, hashed: str) -> bool:
    sha = hashlib.sha256(password.encode('utf-8')).hexdigest()
    return bcrypt.verify(sha, hashed)

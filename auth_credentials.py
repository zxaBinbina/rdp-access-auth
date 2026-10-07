"""Persistent temporary passwords and standard WebAuthn verification."""
import base64
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet
from webauthn import (generate_registration_options, generate_authentication_options,
                     verify_registration_response, verify_authentication_response, options_to_json)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                      UserVerificationRequirement, PublicKeyCredentialDescriptor)

def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')

def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))

def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

def normalized_phrase(value):
    return '-'.join(re.split(r'[\s,，、—－-]+', value.strip()))

class TemporaryPasswords:
    def __init__(self, path, words_path, secret):
        self.path = path
        self.words = tuple(json.loads(Path(words_path).read_text()))
        if len(set(self.words)) < 2048 or any(not re.fullmatch(r'[\u4e00-\u9fff]{2,}', w) for w in self.words):
            raise ValueError('A validated Chinese object wordlist of at least 2048 entries is required')
        key = hmac.new(secret.encode(), b'rdp-auth-temporary-encryption-v1', hashlib.sha256).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS temporary_password (id INTEGER PRIMARY KEY CHECK(id=1), ciphertext TEXT, salt TEXT, verifier TEXT, generation INTEGER, pending TEXT, until INTEGER)')
            if not db.execute('SELECT 1 FROM temporary_password WHERE id=1').fetchone():
                phrase = self.generate()
                salt = secrets.token_hex(16)
                db.execute('INSERT INTO temporary_password VALUES (1,?,?,?,1,NULL,0)',
                    (self.encrypt(phrase), salt, password_hash(phrase, salt)))

    def generate(self):
        return '-'.join(secrets.SystemRandom().sample(self.words, 3))

    def encrypt(self, phrase):
        return self.cipher.encrypt(phrase.encode()).decode('ascii')

    def current(self):
        with closing(sqlite3.connect(self.path)) as db:
            value = db.execute('SELECT ciphertext FROM temporary_password WHERE id=1').fetchone()[0]
        return self.cipher.decrypt(value.encode()).decode()

    def reserve(self, phrase):
        if not isinstance(phrase, str) or len(phrase) > 256:
            return None
        with closing(sqlite3.connect(self.path)) as db:
            salt, verifier, generation = db.execute('SELECT salt,verifier,generation FROM temporary_password WHERE id=1').fetchone()
        if not hmac.compare_digest(password_hash(normalized_phrase(phrase), salt), verifier):
            return None
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT generation,until FROM temporary_password WHERE id=1').fetchone()
            if row[0] != generation:
                return None
            if row[1] > int(time.time()):
                raise RuntimeError('Temporary password is already being used')
            token = secrets.token_urlsafe(24)
            db.execute('UPDATE temporary_password SET pending=?,until=? WHERE id=1', (token, int(time.time()) + 120))
            return token

    def release(self, token):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE temporary_password SET pending=NULL,until=0 WHERE id=1 AND pending=?', (token,))

    def rotate(self, token):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT ciphertext FROM temporary_password WHERE id=1 AND pending=?', (token,)).fetchone()
            if not row:
                raise RuntimeError('Temporary password reservation expired')
            phrase, salt = self.generate(), secrets.token_hex(16)
            while phrase == self.cipher.decrypt(row[0].encode()).decode():
                phrase = self.generate()
            db.execute('UPDATE temporary_password SET ciphertext=?,salt=?,verifier=?,generation=generation+1,pending=NULL,until=0 WHERE id=1',
                       (self.encrypt(phrase), salt, password_hash(phrase, salt)))
            return phrase

    def regenerate(self, expected):
        # Reserving the displayed value prevents stale tabs from invalidating a
        # newer password and never interrupts an authorization in progress.
        token = self.reserve(expected)
        if not token:
            raise RuntimeError('Temporary password changed')
        try:
            return self.rotate(token)
        finally:
            self.release(token)

class Passkeys:
    def __init__(self, path, hostname, secret):
        self.path, self.hostname = path, hostname
        self.origin = 'https://' + hostname
        self.user_id = hmac.new(secret.encode(), b'rdp-auth-passkey-user-v1', hashlib.sha256).digest()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS passkeys (id TEXT PRIMARY KEY, public_key TEXT NOT NULL, sign_count INTEGER NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS passkey_challenges (id TEXT PRIMARY KEY, challenge TEXT, kind TEXT, session_hash TEXT, expires INTEGER)')

    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [dict(id=r[0], name=r[1]) for r in db.execute('SELECT id,name FROM passkeys ORDER BY created')]

    def options(self, kind, session_hash):
        if kind == 'register':
            keys = self.list()
            if len(keys) >= 20:
                raise ValueError('Passkey limit reached')
            opts = generate_registration_options(rp_id=self.hostname, rp_name='个人远程桌面',
                user_name='desktop-owner', user_display_name='我的远程桌面', user_id=self.user_id,
                exclude_credentials=[PublicKeyCredentialDescriptor(id=unb64(k['id'])) for k in keys],
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        else:
            opts = generate_authentication_options(rp_id=self.hostname, user_verification=UserVerificationRequirement.REQUIRED)
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM passkey_challenges WHERE expires <= ? OR (kind=? AND session_hash=?)', (now, kind, session_hash))
            db.execute('INSERT INTO passkey_challenges VALUES (?,?,?,?,?)', (token, b64(opts.challenge), kind, session_hash, now + 120))
        return {'challenge_id': token, 'publicKey': json.loads(options_to_json(opts))}

    def consume(self, token, kind, session_hash):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('DELETE FROM passkey_challenges WHERE id=? AND kind=? AND session_hash=? AND expires>? RETURNING challenge',
                             (token, kind, session_hash, int(time.time()))).fetchone()
        if not row:
            raise ValueError('Expired or already used challenge')
        return unb64(row[0])

    def register(self, token, session_hash, credential, name):
        challenge = self.consume(token, 'register', session_hash)
        verified = verify_registration_response(credential=credential, expected_challenge=challenge,
            expected_rp_id=self.hostname, expected_origin=self.origin, require_user_verification=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM passkeys').fetchone()[0] >= 20:
                raise ValueError('Passkey limit reached')
            db.execute('INSERT INTO passkeys VALUES (?,?,?,?,?)',
                (b64(verified.credential_id), b64(verified.credential_public_key), verified.sign_count,
                 str(name or '我的通行密钥').strip()[:60], int(time.time())))

    def authenticate(self, token, session_hash, credential):
        challenge = self.consume(token, 'authenticate', session_hash)
        credential_id = b64(unb64(credential['id']))
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT public_key,sign_count FROM passkeys WHERE id=?', (credential_id,)).fetchone()
            if not row:
                raise ValueError('Unknown credential')
            user_handle = credential.get('response', {}).get('userHandle')
            if user_handle and not hmac.compare_digest(unb64(user_handle), self.user_id):
                raise ValueError('Unexpected user handle')
            verified = verify_authentication_response(credential=credential, expected_challenge=challenge,
                expected_rp_id=self.hostname, expected_origin=self.origin, credential_public_key=unb64(row[0]),
                credential_current_sign_count=row[1], require_user_verification=True)
            db.execute('UPDATE passkeys SET sign_count=? WHERE id=?', (verified.new_sign_count, credential_id))

    def delete(self, credential_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM passkeys WHERE id=?', (credential_id,))

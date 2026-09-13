"""Versioned encrypted credentials; replacing a token does not revoke pinned runs."""

import base64
import json
import secrets
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select, update

from reviewer.store.models import Account, IntegrationCredential

INTEGRATIONS = {"gateway", "gitlab", "jira", "confluence"}


class CredentialUnavailable(Exception):
    """Sanitized failure: never carry upstream errors or secret values."""


@dataclass(frozen=True)
class CredentialContext:
    user_id: str
    refs: dict
    tokens: dict = field(repr=False)


class Credentials:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def cipher(self, key_id):
        try:
            value = self.settings.credential_keys[key_id].get_secret_value()
            key = base64.b64decode(value, validate=True)
            if len(key) != 32:
                raise ValueError
            return AESGCM(key)
        except (KeyError, ValueError):
            raise CredentialUnavailable(
                "Credential encryption is not configured"
            ) from None

    @staticmethod
    def aad(user_id, integration, version):
        return json.dumps([user_id, integration, version]).encode()

    async def eligible(self, session, user_id):
        account = await session.scalar(
            select(Account).where(Account.id == user_id).with_for_update()
        )
        if (
            account is None
            or not account.active
            or account.role != "user"
            or not account.password_hash
        ):
            raise CredentialUnavailable("Account is not eligible for review execution")
        return account

    async def save(self, user_id, integration, token):
        if integration not in INTEGRATIONS or not token or len(token) > 16384:
            raise CredentialUnavailable("Invalid integration credential")
        key_id = self.settings.credential_active_key
        cipher = self.cipher(key_id)
        async with self.store.transaction() as session:
            await self.eligible(session, user_id)
            version = (
                await session.scalar(
                    select(func.max(IntegrationCredential.version)).where(
                        IntegrationCredential.user_id == user_id,
                        IntegrationCredential.integration == integration,
                    )
                )
                or 0
            ) + 1
            nonce = secrets.token_bytes(12)
            ciphertext = base64.b64encode(
                nonce
                + cipher.encrypt(
                    nonce, token.encode(), self.aad(user_id, integration, version)
                )
            ).decode()
            session.add(
                IntegrationCredential(
                    user_id=user_id,
                    integration=integration,
                    version=version,
                    key_id=key_id,
                    ciphertext=ciphertext,
                )
            )

    async def remove(self, user_id, integration):
        async with self.store.transaction() as session:
            await self.eligible(session, user_id)
            await session.execute(
                update(IntegrationCredential)
                .where(
                    IntegrationCredential.user_id == user_id,
                    IntegrationCredential.integration == integration,
                )
                .values(state="revoked")
            )

    async def latest(self, user_id):
        async with self.store.sessions() as session:
            rows = (
                await session.scalars(
                    select(IntegrationCredential)
                    .where(IntegrationCredential.user_id == user_id)
                    .order_by(IntegrationCredential.version.desc())
                )
            ).all()
            latest = {}
            for row in rows:
                latest.setdefault(row.integration, row)
            return latest

    async def status(self, user_id):
        rows = await self.latest(user_id)
        return {
            name: {
                "status": (
                    row.validation_status
                    if row.validation_status == "invalid"
                    else "configured"
                )
                if (row := rows.get(name)) and row.state == "active"
                else "missing",
                "updated_at": row.created_at if row else None,
                "checked_at": row.checked_at if row else None,
            }
            for name in sorted(INTEGRATIONS)
        }

    async def pin(self, user_id):
        rows = await self.latest(user_id)
        refs = {name: row.id for name, row in rows.items() if row.state == "active"}
        await self.load(user_id, refs)
        return refs

    async def load(self, user_id, refs, required=True, allow_invalid=False):
        if required and not {"gateway", "gitlab"} <= refs.keys():
            raise CredentialUnavailable("Gateway and GitLab credentials are required")
        tokens = {}
        async with self.store.transaction() as session:
            await self.eligible(session, user_id)
            for name, credential_id in refs.items():
                row = await session.get(IntegrationCredential, credential_id)
                if (
                    row is None
                    or row.user_id != user_id
                    or row.integration != name
                    or row.state != "active"
                    or (row.validation_status == "invalid" and not allow_invalid)
                ):
                    raise CredentialUnavailable("Pinned credentials are unavailable")
                try:
                    encrypted = base64.b64decode(row.ciphertext, validate=True)
                    tokens[name] = (
                        self.cipher(row.key_id)
                        .decrypt(
                            encrypted[:12],
                            encrypted[12:],
                            self.aad(user_id, name, row.version),
                        )
                        .decode()
                    )
                except (ValueError, InvalidTag, UnicodeError):
                    raise CredentialUnavailable(
                        "Pinned credentials are unavailable"
                    ) from None
        return CredentialContext(user_id, dict(refs), tokens)

    async def check_access(self, context):
        await self.load(context.user_id, context.refs)

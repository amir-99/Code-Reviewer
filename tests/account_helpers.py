"""Persist real test principals/sessions without repeating password hashing."""

import base64
import secrets
from datetime import timedelta

from reviewer.accounts.credentials import Credentials
from reviewer.accounts.service import digest
from reviewer.store.models import Account, LoginSession, utcnow


async def signed_in(app, role="admin", forge=None):
    settings = app.state.settings
    settings.session_origin = "http://localhost"
    settings.session_local_http = True
    settings.credential_active_key = "test"
    from pydantic import SecretStr

    settings.credential_keys = {"test": SecretStr(base64.b64encode(b"k" * 32).decode())}
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    async with app.state.store.transaction() as session:
        account = Account(
            login=secrets.token_hex(8),
            display_name="Test account",
            role=role,
            password_hash="activated-test-account",
        )
        session.add(account)
        await session.flush()
        session.add(
            LoginSession(
                token_hash=digest(token),
                csrf_hash=digest(csrf),
                user_id=account.id,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
    if role == "user":
        service = Credentials(app.state.store, settings)
        await service.save(account.id, "gateway", "test-gateway-token")
        await service.save(account.id, "gitlab", "test-gitlab-token")
    if forge:
        app.state.personal_forge_factory = lambda *_: forge
    app.state.test_account = account
    return {
        "Cookie": f"reviewer_session={token}",
        "Origin": "http://localhost",
        "X-CSRF-Token": csrf,
    }

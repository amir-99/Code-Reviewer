"""Operator-only first-admin bootstrap; deliver the one-time token privately."""

import argparse
import asyncio

from reviewer.accounts.service import Accounts
from reviewer.config.schema import Settings
from reviewer.store.repositories import Store


async def bootstrap(login, display_name, use_admin_token=False):
    settings = Settings()
    store = Store(settings.database_url.get_secret_value())
    try:
        _, token = await Accounts(store).create(
            login, display_name, "admin", bootstrap=True
        )
        if use_admin_token:
            await Accounts(store).activate(
                token, settings.admin_token.get_secret_value()
            )
            print("Initial admin created with the operator-selected password.")
        else:
            print(f"Activation token (expires in one hour): {token}")
    finally:
        await store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("login")
    parser.add_argument("display_name")
    parser.add_argument(
        "--password-from-admin-token",
        action="store_true",
        help="Explicit operator migration: use the existing ADMIN_TOKEN as the first admin password",
    )
    args = parser.parse_args()
    asyncio.run(
        bootstrap(args.login, args.display_name, args.password_from_admin_token)
    )


if __name__ == "__main__":
    main()

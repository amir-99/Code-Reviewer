"""Operator-only first-admin bootstrap; deliver the one-time token privately."""

import argparse
import asyncio

from reviewer.accounts.service import Accounts
from reviewer.config.schema import Settings
from reviewer.store.repositories import Store


async def bootstrap(login, display_name):
    store = Store(Settings().database_url.get_secret_value())
    try:
        _, token = await Accounts(store).create(
            login, display_name, "admin", bootstrap=True
        )
        print(f"Activation token (expires in one hour): {token}")
    finally:
        await store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("login")
    parser.add_argument("display_name")
    args = parser.parse_args()
    asyncio.run(bootstrap(args.login, args.display_name))


if __name__ == "__main__":
    main()

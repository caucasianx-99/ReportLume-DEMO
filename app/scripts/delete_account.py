"""Operator-only data deletion for a fresh source-edition Firebase project."""

import argparse
import re
import sys

from app.core.firestore_rest import (
    SAFE_ID, FirestoreConflict, FirestoreNotFound, FirestoreUnavailable,
    TenantStore,
)
from app.services.data_lifecycle import (
    AccountDeletionRefused, DataLimit, account_deletion_blocker,
    count_account_documents, count_client_documents, purge_account,
    purge_client,
)


UID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

FAILURES = (
    AccountDeletionRefused, DataLimit, FirestoreConflict,
    FirestoreUnavailable, ValueError,
)


def delete_sign_in(uid):
    """Remove the Firebase Authentication user; a missing user is fine."""
    from firebase_admin import auth
    from app.core.firebase import get_firebase_app

    try:
        auth.delete_user(uid, app=get_firebase_app())
    except auth.UserNotFoundError:
        return False
    return True


def main(argv=None, *, store_factory=TenantStore, remove_sign_in=delete_sign_in):
    parser = argparse.ArgumentParser(
        description="Delete a Reportlume account or one of its clients"
    )
    parser.add_argument("--uid", required=True, help="Firebase user identifier")
    parser.add_argument("--client", help="Delete only this client")
    parser.add_argument(
        "--apply", action="store_true",
        help="Delete; without it the command only counts",
    )
    parser.add_argument(
        "--keep-sign-in", action="store_true",
        help="Account deletion: keep the Firebase sign-in account",
    )
    args = parser.parse_args(argv)

    if not UID.fullmatch(args.uid):
        print("Invalid user identifier", file=sys.stderr)
        return 1
    if args.client is not None and not SAFE_ID.fullmatch(args.client):
        print("Invalid client identifier", file=sys.stderr)
        return 1

    try:
        store = store_factory(args.uid)

        if args.client is not None:
            if not args.apply:
                count = count_client_documents(store, args.client)
                print(f"dry-run client documents={count}")
                return 0
            deleted = purge_client(store, args.client)
            print(f"deleted client documents={deleted}")
            return 0

        reason = account_deletion_blocker(store)
        if reason:
            print("Account deletion refused:", reason, file=sys.stderr)
            return 1

        if not args.apply:
            count = count_account_documents(store)
            print(f"dry-run account documents={count}")
            return 0

        signed_out = False
        if not args.keep_sign_in:
            signed_out = remove_sign_in(args.uid)
        deleted = purge_account(store)
        print(
            f"deleted account documents={deleted} "
            f"sign_in_removed={'yes' if signed_out else 'no'}"
        )
        return 0

    except FirestoreNotFound:
        print("Nothing to delete", file=sys.stderr)
        return 1
    except FAILURES as exc:
        print("Deletion stopped:", type(exc).__name__, file=sys.stderr)
        print("Run the same command again to continue.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

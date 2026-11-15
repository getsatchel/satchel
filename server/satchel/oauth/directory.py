"""Accounts at the identity provider, for invites (``SATCHEL_OIDC_CREATE_USERS``).

An invite in Satchel is a member with an email; the person still needs an account at the
identity provider to sign in. With ``oidc_create_users`` on, inviting someone also creates that
account, and the provider emails them how to sign in. Only Amazon Cognito is supported: the
issuer ``https://cognito-idp.<region>.amazonaws.com/<user-pool-id>`` names the pool.

``boto3`` (extra ``aws``) is imported lazily; credentials come from the standard AWS chain (an
instance role, ``AWS_*`` variables) and need ``cognito-idp:AdminCreateUser`` on the pool.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from satchel.settings import Settings

AccountStatus = Literal["created", "exists", "failed"]
COGNITO_ISSUER = re.compile(r"^cognito-idp\.([a-z0-9-]+)\.amazonaws\.com$")


@dataclass(frozen=True)
class AccountResult:
    email: str
    status: AccountStatus
    detail: str | None = None


class Directory(Protocol):
    name: str

    def create_user(self, email: str) -> AccountResult: ...


class CognitoDirectory:
    name = "Cognito"

    def __init__(self, region: str, pool_id: str, client: Any = None) -> None:
        self.region, self.pool_id, self._client = region, pool_id, client

    def client(self) -> Any:
        if self._client is None:
            import boto3  # optional dependency (extra "aws")

            self._client = boto3.client("cognito-idp", region_name=self.region)
        return self._client

    def create_user(self, email: str) -> AccountResult:
        c = self.client()
        try:
            c.admin_create_user(
                UserPoolId=self.pool_id, Username=email,
                UserAttributes=[{"Name": "email", "Value": email},
                                {"Name": "email_verified", "Value": "true"}],
                DesiredDeliveryMediums=["EMAIL"],
            )
        except c.exceptions.UsernameExistsException:
            return AccountResult(email, "exists")
        except Exception as e:  # noqa: BLE001 - reported per address; the invite still stands
            return AccountResult(email, "failed", str(e)[:300])
        return AccountResult(email, "created")


def cognito_pool(issuer: str | None) -> tuple[str, str] | None:
    """(region, user pool id) when the issuer is a Cognito user pool, else None."""
    u = urlsplit(issuer or "")
    m = COGNITO_ISSUER.match((u.hostname or "").lower())
    pool = u.path.strip("/")
    if not m or not re.fullmatch(r"[\w-]+_[0-9A-Za-z]+", pool):
        return None
    return m.group(1), pool


def directory_for(settings: Settings) -> Directory | None:
    """The directory invites create accounts in, or None when that is off or unsupported."""
    if not settings.oidc_create_users:
        return None
    pool = cognito_pool(settings.oidc_issuer)
    return CognitoDirectory(*pool) if pool else None

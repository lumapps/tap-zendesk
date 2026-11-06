"""OAuth client credentials authentication for Zendesk.

Zendesk now expires OAuth access tokens (30 min by default, 48 h max), so a
static ``access_token`` stops working. This module fetches access tokens with
the ``client_credentials`` grant and refreshes them transparently:

* proactively, shortly before the token expires;
* reactively, once, when the API answers ``401``.

See https://developer.zendesk.com/api-reference/ticketing/oauth/grant_type_tokens/
"""
import time

import requests
from requests.auth import AuthBase
import singer

LOGGER = singer.get_logger()

DEFAULT_SCOPE = "read"
# Zendesk default access token lifetime, used when the response omits it.
DEFAULT_EXPIRES_IN = 1800
# Refresh the token this many seconds before it expires.
EXPIRY_MARGIN = 120
TOKEN_REQUEST_TIMEOUT = 30


class ZendeskOAuthError(Exception):
    pass


class ClientCredentialsAuth(AuthBase):
    """requests auth handler fetching and refreshing a Zendesk OAuth access token."""

    def __init__(self, subdomain, client_id, client_secret, scope=DEFAULT_SCOPE, expires_in=None):
        self.token_url = f"https://{subdomain}.zendesk.com/oauth/tokens"
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.expires_in = int(expires_in) if expires_in else None
        self.access_token = None
        self.expires_at = 0

    def _token_is_valid(self):
        return self.access_token is not None and time.time() < self.expires_at - EXPIRY_MARGIN

    def refresh_token(self):
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        if self.expires_in:
            payload["expires_in"] = self.expires_in

        response = requests.post(self.token_url, json=payload, timeout=TOKEN_REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise ZendeskOAuthError(
                f"Failed to fetch Zendesk OAuth access token ({response.status_code}): {response.text}")

        body = response.json()
        if "access_token" not in body:
            raise ZendeskOAuthError("Zendesk OAuth token response has no access_token.")

        expires_in = body.get("expires_in") or self.expires_in or DEFAULT_EXPIRES_IN
        self.access_token = body["access_token"]
        self.expires_at = time.time() + int(expires_in)
        LOGGER.info("Fetched Zendesk OAuth access token (expires in %ss).", expires_in)

    def get_token(self):
        if not self._token_is_valid():
            self.refresh_token()
        return self.access_token

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self.get_token()}"
        request.register_hook("response", self._retry_on_401)
        return request

    def _retry_on_401(self, response, **kwargs):
        if response.status_code != 401 or getattr(response.request, "_oauth_retried", False):
            return response

        LOGGER.warning("Zendesk answered 401, refreshing OAuth access token and retrying.")
        self.refresh_token()

        # Release the connection before resending.
        _ = response.content
        response.close()

        retry = response.request.copy()
        retry.headers["Authorization"] = f"Bearer {self.access_token}"
        retry._oauth_retried = True  # pylint: disable=protected-access

        new_response = response.connection.send(retry, **kwargs)
        new_response.history.append(response)
        new_response.request = retry
        return new_response

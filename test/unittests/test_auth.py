import time
import unittest
from argparse import Namespace
from unittest import mock

import requests

from tap_zendesk import get_client, MissingCredentialsException
from tap_zendesk.auth import ClientCredentialsAuth, ZendeskOAuthError, EXPIRY_MARGIN


def token_response(token, expires_in=1800, status=200):
    response = mock.Mock(status_code=status, text="err")
    response.json.return_value = {"access_token": token, "token_type": "bearer", "expires_in": expires_in}
    return response


class TestClientCredentialsAuth(unittest.TestCase):

    def setUp(self):
        self.auth = ClientCredentialsAuth("acme", "cid", "secret")

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_fetches_token_with_client_credentials(self, post):
        post.return_value = token_response("tok1")
        request = requests.Request("GET", "https://acme.zendesk.com/api/v2/tickets.json").prepare()

        self.auth(request)

        self.assertEqual(request.headers["Authorization"], "Bearer tok1")
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://acme.zendesk.com/oauth/tokens")
        self.assertEqual(post.call_args[1]["json"], {
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "secret",
            "scope": "read",
        })

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_sends_expires_in_when_configured(self, post):
        post.return_value = token_response("tok1", expires_in=172800)
        auth = ClientCredentialsAuth("acme", "cid", "secret", scope="read", expires_in="172800")

        auth.get_token()

        self.assertEqual(post.call_args[1]["json"]["expires_in"], 172800)

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_reuses_valid_token(self, post):
        post.return_value = token_response("tok1")

        self.auth.get_token()
        self.auth.get_token()

        post.assert_called_once()

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_refreshes_token_before_expiry(self, post):
        post.side_effect = [token_response("tok1"), token_response("tok2")]

        self.assertEqual(self.auth.get_token(), "tok1")
        self.auth.expires_at = time.time() + EXPIRY_MARGIN - 1
        self.assertEqual(self.auth.get_token(), "tok2")

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_raises_on_token_error(self, post):
        post.return_value = token_response(None, status=401)

        with self.assertRaises(ZendeskOAuthError):
            self.auth.get_token()

    @mock.patch("tap_zendesk.auth.requests.post")
    def test_retries_once_on_401(self, post):
        post.side_effect = [token_response("tok1"), token_response("tok2")]
        request = requests.Request("GET", "https://acme.zendesk.com/api/v2/tickets.json").prepare()
        self.auth(request)

        retried = mock.Mock(status_code=200, history=[])
        unauthorized = mock.Mock(status_code=401, request=request)
        unauthorized.connection.send.return_value = retried

        result = self.auth._retry_on_401(unauthorized)

        self.assertIs(result, retried)
        sent = unauthorized.connection.send.call_args[0][0]
        self.assertEqual(sent.headers["Authorization"], "Bearer tok2")
        self.assertTrue(sent._oauth_retried)

        # a second 401 on the retried request is returned as is
        retried_unauthorized = mock.Mock(status_code=401, request=sent)
        self.assertIs(self.auth._retry_on_401(retried_unauthorized), retried_unauthorized)


class TestGetClient(unittest.TestCase):

    def test_client_credentials_have_precedence(self):
        config = {"subdomain": "acme", "client_id": "cid", "client_secret": "secret", "access_token": "static"}
        client = get_client(Namespace(config=config))

        session = client.tickets.session
        self.assertIsInstance(session.auth, ClientCredentialsAuth)
        self.assertNotIn("Authorization", session.headers)

    def test_falls_back_to_access_token(self):
        client = get_client(Namespace(config={"subdomain": "acme", "access_token": "static"}))

        self.assertEqual(client.tickets.session.headers["Authorization"], "Bearer static")

    def test_raises_without_credentials(self):
        with self.assertRaises(MissingCredentialsException):
            get_client(Namespace(config={"subdomain": "acme"}))

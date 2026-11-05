# twilio-tap-zendesk
Tap for Zendesk

## Installation

1. Create and activate a virtualenv
1. `pip install -e '.[dev]'`

---

## Authentication

Precedence when several methods are configured: OAuth client credentials > OAuth access token > API token.

### Using OAuth client credentials (recommended)

Zendesk now expires OAuth tokens (access: 30 min default / 48 h max, refresh: 30 days default / 90 days max), enforced on all clients by 2027-04-01. A static `access_token` will stop working.

With `client_id` / `client_secret` of a Zendesk OAuth client, the tap fetches an access token through the [`client_credentials` grant](https://developer.zendesk.com/api-reference/ticketing/oauth/grant_type_tokens/) at startup, refreshes it before it expires, and retries once on `401` with a fresh token.

**config.json**
```json
{
  "client_id": "my_oauth_client",
  "client_secret": "AVERYLONGSECRET",
  "subdomain": "acme",
  "start_date": "2000-01-01T00:00:00Z"
}
```

Optional:
- `oauth_scope`: token scope, defaults to `read`.
- `oauth_expires_in`: requested access token lifetime in seconds (300 to 172800). Zendesk default (1800) if omitted.

### Using OAuth access token

To use a static OAuth access token, you will need to fetch an `access_token` from a configured Zendesk integration. See https://support.zendesk.com/hc/en-us/articles/203663836 for more details on how to integrate your application with Zendesk.

**config.json**
```json
{
  "access_token": "AVERYLONGOAUTHTOKEN",
  "subdomain": "acme",
  "start_date": "2000-01-01T00:00:00Z"
}


```

### Using API Tokens

For a simplified, but less granular setup, you can use the API Token authentication which can be generated from the Zendesk Admin page. See https://support.zendesk.com/hc/en-us/articles/226022787-Generating-a-new-API-token- for more details about generating an API Token. You'll then be able to use the admins's `email` and the generated `api_token` to authenticate.

**config.json**
```json
{
  "email": "user@domain.com",
  "api_token": "THISISAVERYLONGTOKEN",
  "subdomain": "acme",
  "start_date": "2000-01-01T00:00:00Z"
}
```

An optional `end_date` field can be added to the `config.json`
This functionality has been added to ease the backfill procedure for a limited time duration for Zendesk

If passed, data would be loaded for `date >= start_date and date < end_date`

A second, optional, configurable field `skip_deleted` can also be passed in the config. This defaults to `True`, and means that if a ticket's status field is `deleted`, the audits, metrics and comments will not be requested for that ticket ID. 

Note that the `status = deleted` flag is only valid for the `/incremental/tickets` endpoint, not `/tickets`.

### Sideloading for tickets

Sideloading is a functionality provided by Zendesk to fetch related records in a single request 
more detail - https://developer.zendesk.com/documentation/ticketing/using-the-zendesk-api/side_loading/

There are two ways in which the records are returned:

1. Within the same object as a column
2. As a separate object 

Here we have provided support for sideloading only the 1st type i.e. the attributes returned in the same object as additional columns

Sideload supported for tickets:
1. comment_count
2. dates
3. metric_sets
4. slas

To sideload an object a list can be passed in the metadata under `sideload-objects` for tickets object in the catalog.json file

e.g.
```json
{"metadata": [
        {
          "breadcrumb": [],
          "metadata": {
            "table-key-properties": [
              "id"
            ],
            "forced-replication-method": "INCREMENTAL",
            "valid-replication-keys": [
              "generated_timestamp"
            ],
            "sideload-objects": ["comment_count","dates","metric_events","slas"]
          }
        }]
}
```

note: above extract is a part of the complete metadata for tickets 

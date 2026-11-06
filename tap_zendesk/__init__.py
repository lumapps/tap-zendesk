#!/usr/bin/env python3
import json
import sys, os
from datetime import datetime
from zenpy import Zenpy
import requests
from requests import Session
from requests.adapters import HTTPAdapter
import singer
from singer import metadata, Schema, metrics as singer_metrics
from tap_zendesk import metrics as zendesk_metrics
from tap_zendesk.auth import ClientCredentialsAuth, DEFAULT_SCOPE
from tap_zendesk.discover import discover_streams
from tap_zendesk.streams import STREAMS
from tap_zendesk.sync import sync_stream

LOGGER = singer.get_logger()

REQUIRED_CONFIG_KEYS = [
    "start_date",
    "subdomain",
]

# OAuth client credentials authentication (recommended, tokens are refreshed automatically)
CLIENT_CREDENTIALS_CONFIG_KEYS = [
    "client_id",
    "client_secret",
]

# static OAuth access token authentication (deprecated by Zendesk token expiration)
OAUTH_CONFIG_KEYS = [
    "access_token",
]

# email + api_token authentication
API_TOKEN_CONFIG_KEYS = [
    "email",
    "api_token",
]

# patch Session.request to record HTTP request metrics
request = Session.request


def request_metrics_patch(self, method, url, **kwargs):
    with singer_metrics.http_request_timer(None):
        return request(self, method, url, **kwargs)

Session.request = request_metrics_patch
# end patch

def do_discover(client):
    LOGGER.info("Starting discover")
    catalog = {"streams": discover_streams(client)}
    json.dump(catalog, sys.stdout, indent=2)
    LOGGER.info("Finished discover")


def stream_is_selected(mdata):
    return mdata.get((), {}).get('selected', False)


def get_selected_streams(catalog):
    selected_stream_names = []
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        if stream_is_selected(mdata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names


SUB_STREAMS = {
    'tickets': ['ticket_audits', 'ticket_metrics', 'ticket_comments']
}

# only side loading objects that are returned as a child object and not a separate array
SIDELOAD_OBJECTS = {
    'tickets': ['metric_sets', 'dates', 'comment_count', 'slas']
}


def get_sub_stream_names():
    sub_stream_names = []
    for parent_stream in SUB_STREAMS:
        sub_stream_names.extend(SUB_STREAMS[parent_stream])
    return sub_stream_names


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def get_side_load_schemas(sideload_objects, stream):
    """Returns the updated schema after adding side load objects to schema dict"""
    stream_schema = stream.schema.to_dict()
    for sideload_object in sideload_objects:
        if stream.tap_stream_id in SIDELOAD_OBJECTS:
            if sideload_object in SIDELOAD_OBJECTS[stream.tap_stream_id]:
                schema_file = "schemas/sideload_schemas/{}.json".format(sideload_object)
                with open(get_abs_path(schema_file)) as f:
                    schema = json.load(f)
                    stream_schema['properties'][list(schema['properties'].keys())[0]] = list(schema['properties'].values())[0]
    return stream_schema


class DependencyException(Exception):
    pass


class MissingCredentialsException(Exception):
    pass


def validate_dependencies(selected_stream_ids):
    errs = []
    msg_tmpl = ("Unable to extract {0} data. "
                "To receive {0} data, you also need to select {1}.")
    for parent_stream_name in SUB_STREAMS:
        sub_stream_names = SUB_STREAMS[parent_stream_name]
        for sub_stream_name in sub_stream_names:
            if sub_stream_name in selected_stream_ids and parent_stream_name not in selected_stream_ids:
                errs.append(msg_tmpl.format(sub_stream_name, parent_stream_name))

    if errs:
        raise DependencyException(" ".join(errs))


def populate_class_schemas(catalog, selected_stream_names):
    for stream in catalog.streams:
        if stream.tap_stream_id in selected_stream_names:
            STREAMS[stream.tap_stream_id].stream = stream


def do_sync(client, catalog, state, config):
    selected_stream_names = get_selected_streams(catalog)
    validate_dependencies(selected_stream_names)
    populate_class_schemas(catalog, selected_stream_names)
    all_sub_stream_names = get_sub_stream_names()

    for stream in catalog.streams:
        stream_name = stream.tap_stream_id
        mdata = metadata.to_map(stream.metadata)
        if stream_name not in selected_stream_names:
            LOGGER.info("%s: Skipping - not selected", stream_name)
            continue

        key_properties = metadata.get(mdata, (), 'table-key-properties')
        sideload_objects = metadata.get(mdata, (), 'sideload-objects')
        if sideload_objects:
            stream_schema = get_side_load_schemas(sideload_objects, stream)
            stream.schema = Schema.from_dict(stream_schema)

        singer.write_schema(stream_name, stream.schema.to_dict(), key_properties)

        sub_stream_names = SUB_STREAMS.get(stream_name)
        if sub_stream_names:
            for sub_stream_name in sub_stream_names:
                if sub_stream_name not in selected_stream_names:
                    continue
                sub_stream = STREAMS[sub_stream_name].stream
                sub_mdata = metadata.to_map(sub_stream.metadata)
                sub_key_properties = metadata.get(sub_mdata, (), 'table-key-properties')
                sideload_objects = metadata.get(mdata, (), 'sideload-objects')
                if sideload_objects:
                    sub_stream_schema = get_side_load_schemas(sideload_objects, sub_stream)
                    sub_stream.schema = Schema.from_dict(sub_stream_schema)
                singer.write_schema(sub_stream.tap_stream_id, sub_stream.schema.to_dict(), sub_key_properties)

        # parent stream will sync sub stream
        if stream_name in all_sub_stream_names:
            continue

        LOGGER.info("%s: Starting sync", stream_name)
        instance = STREAMS[stream_name](client, config)
        counter_value = sync_stream(state, config.get('start_date'), instance)
        singer.write_state(state)
        LOGGER.info("%s: Completed sync (%s rows)", stream_name, counter_value)
        zendesk_metrics.log_aggregate_rates()

    singer.write_state(state)
    LOGGER.info("Finished sync")
    zendesk_metrics.log_aggregate_rates()


def client_credentials_auth(config):
    if not set(CLIENT_CREDENTIALS_CONFIG_KEYS).issubset(config.keys()):
        LOGGER.debug("OAuth client credentials authentication unavailable.")
        return None

    LOGGER.info("Using OAuth client credentials authentication.")
    return ClientCredentialsAuth(
        subdomain=config['subdomain'],
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        scope=config.get('oauth_scope', DEFAULT_SCOPE),
        expires_in=config.get('oauth_expires_in'),
    )


def oauth_auth(args):
    if not set(OAUTH_CONFIG_KEYS).issubset(args.config.keys()):
        LOGGER.debug("OAuth authentication unavailable.")
        return None

    LOGGER.info("Using OAuth authentication.")
    return {
        "subdomain": args.config['subdomain'],
        "oauth_token": args.config['access_token'],
    }


def api_token_auth(args):
    if not set(API_TOKEN_CONFIG_KEYS).issubset(args.config.keys()):
        LOGGER.debug("API Token authentication unavailable.")
        return None

    LOGGER.info("Using API Token authentication.")
    return {
        "subdomain": args.config['subdomain'],
        "email": args.config['email'],
        "token": args.config['api_token']
    }


def get_session(config):
    """ Add partner information to requests Session object if specified in the config. """
    if not all(k in config for k in ["marketplace_name",
                                     "marketplace_organization_id",
                                     "marketplace_app_id"]):
        return None
    session = requests.Session()
    # Using Zenpy's default adapter args, following the method outlined here:
    # https://github.com/facetoe/zenpy/blob/master/docs/zenpy.rst#usage
    session.mount("https://", HTTPAdapter(**Zenpy.http_adapter_kwargs()))
    session.headers["X-Zendesk-Marketplace-Name"] = config.get("marketplace_name", "")
    session.headers["X-Zendesk-Marketplace-Organization-Id"] = str(config.get("marketplace_organization_id", ""))
    session.headers["X-Zendesk-Marketplace-App-Id"] = str(config.get("marketplace_app_id", ""))
    return session


def get_client(args):
    """ Build the Zenpy client. Precedence: client credentials > OAuth access token > API token. """
    session = get_session(args.config)

    cc_auth = client_credentials_auth(args.config)
    if cc_auth:
        if session is None:
            session = requests.Session()
            session.mount("https://", HTTPAdapter(**Zenpy.http_adapter_kwargs()))
        session.auth = cc_auth
        # Tell Zenpy the session is already authorized so it doesn't set its own credentials.
        session.authorized = True
        return Zenpy(subdomain=args.config['subdomain'], session=session)

    creds = oauth_auth(args) or api_token_auth(args)
    if not creds:
        raise MissingCredentialsException(
            "No suitable authentication keys provided. Expected one of: "
            f"{CLIENT_CREDENTIALS_CONFIG_KEYS}, {OAUTH_CONFIG_KEYS} or {API_TOKEN_CONFIG_KEYS}.")
    return Zenpy(session=session, **creds)


@singer.utils.handle_top_exception(LOGGER)
def main():
    parsed_args = singer.utils.parse_args(REQUIRED_CONFIG_KEYS)
    client = get_client(parsed_args)

    if parsed_args.discover:
        do_discover(client)
    elif parsed_args.catalog:
        state = parsed_args.state
        do_sync(client, parsed_args.catalog, state, parsed_args.config)

"""API key checking on the WebSocket."""

import pytest

from backend.auth import (
    SUBPROTOCOL_PREFIX,
    extract_key,
    is_authorised,
    selected_subprotocol,
)
from backend.config import Settings


# --- where the key comes from ---------------------------------------------


def test_key_is_read_from_a_bearer_header():
    assert extract_key("Bearer secret123", None) == "secret123"


def test_bearer_scheme_is_matched_case_insensitively():
    assert extract_key("bearer secret123", None) == "secret123"
    assert extract_key("BEARER secret123", None) == "secret123"


def test_other_auth_schemes_are_ignored():
    """Only Bearer. A Basic header is not a key we issued."""
    assert extract_key("Basic secret123", None) is None


def test_bearer_with_no_value_is_not_a_key():
    assert extract_key("Bearer", None) is None
    assert extract_key("Bearer   ", None) is None


def test_key_is_read_from_the_websocket_subprotocol():
    """How a browser sends it: headers cannot be set on a WS handshake."""
    assert extract_key(None, [f"{SUBPROTOCOL_PREFIX}secret123"]) == "secret123"


def test_unrelated_subprotocols_are_ignored():
    assert extract_key(None, ["graphql-ws", "json"]) is None


def test_the_key_is_found_among_other_offered_subprotocols():
    protocols = ["json", f"{SUBPROTOCOL_PREFIX}secret123", "other"]
    assert extract_key(None, protocols) == "secret123"


def test_header_wins_when_both_are_present():
    key = extract_key("Bearer from-header", [f"{SUBPROTOCOL_PREFIX}from-protocol"])
    assert key == "from-header"


def test_no_key_offered_at_all():
    assert extract_key(None, None) is None
    assert extract_key(None, []) is None


# --- the check ------------------------------------------------------------


def test_no_configured_keys_leaves_the_socket_open():
    """Local development: the check is off until keys are configured."""
    assert is_authorised(None, []) is True
    assert is_authorised("anything", []) is True


def test_a_matching_key_is_accepted():
    assert is_authorised("k1", ["k1", "k2"]) is True
    assert is_authorised("k2", ["k1", "k2"]) is True


def test_a_wrong_key_is_rejected():
    assert is_authorised("k3", ["k1", "k2"]) is False


def test_a_missing_key_is_rejected_once_keys_are_configured():
    assert is_authorised(None, ["k1"]) is False
    assert is_authorised("", ["k1"]) is False


def test_a_key_that_is_a_prefix_of_a_valid_one_is_rejected():
    """Guards against a partial match being treated as good enough."""
    assert is_authorised("secret", ["secret123"]) is False


def test_multiple_keys_allow_rotation():
    """Two valid keys at once is what makes a key rotatable without downtime."""
    accepted = ["old-key", "new-key"]
    assert is_authorised("old-key", accepted)
    assert is_authorised("new-key", accepted)


# --- the subprotocol echo -------------------------------------------------


def test_the_offered_key_subprotocol_is_echoed():
    """A browser closes the connection if the server names nothing back."""
    offered = [f"{SUBPROTOCOL_PREFIX}secret123"]
    assert selected_subprotocol(offered) == f"{SUBPROTOCOL_PREFIX}secret123"


def test_nothing_is_echoed_when_no_key_subprotocol_was_offered():
    assert selected_subprotocol(["json"]) is None
    assert selected_subprotocol(None) is None


# --- configuration --------------------------------------------------------


def test_keys_parse_from_a_comma_separated_string():
    settings = Settings(api_keys="  one , two ,, three ")
    assert settings.api_key_list == ["one", "two", "three"]


def test_no_keys_configured_is_an_empty_list():
    assert Settings(api_keys="").api_key_list == []
    assert Settings().api_key_list == []

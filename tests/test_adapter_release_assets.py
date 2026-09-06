"""ADP-001 — ForgeAdapter gains upload_release_asset, the method that keeps a
release's built assets attached to its release object on each forge.

The proven upload shapes come from the working production workflow
``.forgejo/workflows/forgejo-release.yml`` (the "Upload assets" and "Upload
assets to GitHub" steps), which this lane does not rerun; it moves those two
curl shapes into the adapter contract and proves them against a stubbed HTTP
layer, so a later phase can drive asset upload through the engine instead of
through curl in a workflow.

END-ZONE SHAPES (read from the workflow, not invented):
  Forgejo  POST {api_base}/api/v1/repos/{repo}/releases/{id}/assets?name={name}
           content-type application/octet-stream, body --data-binary
  GitHub   POST https://uploads.github.com/repos/{repo}/releases/{id}/assets?name={name}
           content-type application/octet-stream, body --data-binary

Every assertion here exercises a real call with a stubbed HTTP transport: the
adapter's own ``_request`` retry/error pipeline runs for real, only the socket
is replaced by an ``httpx``-shaped mock.
"""

import httpx
import pytest
from unittest.mock import MagicMock, AsyncMock

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter, UPLOADS_HOST

ASSET_BYTES = b"\x00wheel-bytes\xff"

CONTENT_TYPE = "application/octet-stream"


# --- a stub HTTP transport (replaces the socket, keeps _request running) -----


def _install_fake_client(adapter, response):
    """Attach a fake async client whose single ``request`` returns ``response``
    and records the outgoing call for assertions."""
    client = MagicMock()
    client.is_closed = False
    recorded = {}
    async def request(method, url_or_path, **kwargs):
        recorded.update({"method": method, "url": url_or_path})
        recorded.update({k: v for k, v in kwargs.items()})
        return response
    client.request = request
    adapter._client = client
    return client, recorded


def _ok_response(payload: dict):
    resp = MagicMock()
    resp.status_code = 201
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _error_httpx_response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://example.invalid/upload")
    return httpx.Response(status_code, request=request)


@pytest.fixture
def github_adapter():
    return GithubAdapter(token="test-token", webhook_secret="secret")


@pytest.fixture
def forgejo_adapter():
    return ForgejoAdapter(
        base_url="https://git.langevc.com", token="test-token", webhook_secret="secret"
    )


# --- Criterion 1: the contract method and the abstract-enforcement proof -------


def test_upload_release_asset_is_declared_abstract():
    assert isinstance(
        ForgeAdapter.__abstractmethods__,
        frozenset,
    ), "ForgeAdapter must keep its abstractmethods intact"
    assert "upload_release_asset" in ForgeAdapter.__abstractmethods__


def _concrete_subclass_omitting_upload():
    """Build a ForgeAdapter subclass that implements every inherited abstract
    method EXCEPT ``upload_release_asset``, which is deliberately left out so
    the class stays abstract for exactly that one method."""
    methods = {}
    for name in ForgeAdapter.__abstractmethods__:
        if name == "upload_release_asset":
            continue
        async def _stub(self, *args, **kwargs):
            raise AssertionError("stub " + name + " must not be reached")
        _stub.__name__ = name
        methods[name] = _stub
    return type("OmitsUpload", (ForgeAdapter,), methods)


def test_subclass_that_omits_upload_release_asset_raises_type_error():
    """Constructing a subclass that omits the new abstract method raises
    TypeError, leaving exactly ``upload_release_asset`` abstract."""
    cls = _concrete_subclass_omitting_upload()
    assert cls.__abstractmethods__ == frozenset({"upload_release_asset"})
    with pytest.raises(TypeError):
        cls()


def test_both_concrete_adapters_implement_the_method(github_adapter, forgejo_adapter):
    assert "upload_release_asset" in GithubAdapter.__dict__
    assert "upload_release_asset" in ForgejoAdapter.__dict__
    assert isinstance(github_adapter, ForgeAdapter)
    assert isinstance(forgejo_adapter, ForgeAdapter)


# --- Criterion 2: GitHub upload host, path, method, content type, failure -----


@pytest.mark.asyncio
async def test_github_upload_builds_correct_method_host_path_and_type(github_adapter):
    payload = {"id": 900, "name": "ops_engine-3.2.0-py3-none-any.whl"}
    client, recorded = _install_fake_client(github_adapter, _ok_response(payload))

    result = await github_adapter.upload_release_asset(
        "langevc/ops-engine", 900, "ops_engine-3.2.0-py3-none-any.whl",
        ASSET_BYTES, content_type=CONTENT_TYPE,
    )

    assert result == payload
    assert recorded["method"] == "POST"
    # GitHub asset upload goes to the SEPARATE uploads host, not api.github.com.
    assert recorded["url"].startswith(UPLOADS_HOST + "/")
    assert "/repos/langevc/ops-engine/releases/900/assets" in recorded["url"]
    assert recorded["url"].endswith("assets")
    assert recorded["params"] == {"name": "ops_engine-3.2.0-py3-none-any.whl"}
    assert recorded["content"] == ASSET_BYTES
    assert recorded["headers"]["Content-Type"] == CONTENT_TYPE


@pytest.mark.asyncio
async def test_github_upload_failure_surfaces_named_http_error(github_adapter):
    response = _error_httpx_response(422)
    _install_fake_client(github_adapter, response)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await github_adapter.upload_release_asset(
            "langevc/ops-engine", 900, "asset.whl", ASSET_BYTES,
        )
    assert exc_info.value.response.status_code == 422


# --- Criterion 2: Forgejo upload host, path, method, content type, failure ---


@pytest.mark.asyncio
async def test_forgejo_upload_builds_correct_method_host_path_and_type(forgejo_adapter):
    payload = {"id": 42, "name": "SHA256SUMS"}
    client, recorded = _install_fake_client(forgejo_adapter, _ok_response(payload))

    result = await forgejo_adapter.upload_release_asset(
        "langevc/ops-engine", 42, "SHA256SUMS", ASSET_BYTES, content_type=CONTENT_TYPE,
    )

    assert result == payload
    assert recorded["method"] == "POST"
    # Forgejo attaches on the SAME API host the adapter talks to (base_url),
    # under the /api/v1 prefix the adapter's client provides as its base_url.
    # The relative path the adapter passed must be the proven forgejo shape.
    assert recorded["url"] == "/repos/langevc/ops-engine/releases/42/assets"
    assert recorded["params"] == {"name": "SHA256SUMS"}
    assert recorded["content"] == ASSET_BYTES
    assert recorded["headers"]["Content-Type"] == CONTENT_TYPE
    assert forgejo_adapter.base_url == "https://git.langevc.com"


@pytest.mark.asyncio
async def test_forgejo_upload_failure_surfaces_named_http_error(forgejo_adapter):
    response = _error_httpx_response(500)
    _install_fake_client(forgejo_adapter, response)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await forgejo_adapter.upload_release_asset(
            "langevc/ops-engine", 42, "sha256", ASSET_BYTES,
        )
    assert exc_info.value.response.status_code == 500

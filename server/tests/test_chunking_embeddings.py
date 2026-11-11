import httpx

from satchel.core.tokens import approx_tokens
from satchel.llm.embeddings import HashEmbedder, OpenAICompatibleProvider
from satchel.sync.chunking import MAX_TOKENS, chunk_markdown


def test_chunk_markdown_splits_on_headings_and_limits_size() -> None:
    para = "word " * 120
    md = "# Title\n\nintro\n\n" + "".join(f"## Section {i}\n\n{para}\n\n" for i in range(8))
    chunks = chunk_markdown(md, "Guide: x")
    assert len(chunks) > 1
    assert all(c.startswith("Guide: x") for c in chunks)
    assert all(approx_tokens(c) <= MAX_TOKENS + 20 for c in chunks)
    assert chunk_markdown("# Only\n\nshort") == ["# Only\n\nshort"]


def test_hash_embedder_deterministic_unit_vectors() -> None:
    e = HashEmbedder()
    a, b = e.embed(["monthly revenue", "monthly revenue"])
    assert a == b and len(a) == e.dim == 1024
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6
    sim = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))  # noqa: E731
    assert sim(a, e.embed_query("monthly revenue report")) > sim(a, e.embed_query("zebra"))


def test_openai_compat_provider_uses_endpoint() -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(req.content)
        seen.append({"auth": req.headers["authorization"], "url": str(req.url), **body})
        data = [
            {"index": i, "embedding": [float(i)] * 4} for i in reversed(range(len(body["input"])))
        ]
        return httpx.Response(200, json={"data": data})

    p = OpenAICompatibleProvider(
        "http://tei/v1/",
        "bge",
        "k",
        dim=4,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    out = p.embed(["a", "b"])
    assert out == [[0.0] * 4, [1.0] * 4]
    assert seen[0]["auth"] == "Bearer k" and seen[0]["url"] == "http://tei/v1/embeddings"
    assert p.model_id == "openai_compat:bge"

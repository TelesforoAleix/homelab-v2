import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from llama_index.core.schema import MetadataMode

from homelab.api.app import KnowledgeAnswerResponse, app
from homelab.knowledge import index, sources
from homelab.knowledge.evaluate import evaluate
from homelab.settings import Settings


def corpus(tmp_path):
    entries = [
        dict(
            id=f"fixture-{n:03}",
            question=f"Which fixture {n}?",
            answer=f"Fixture {n}.",
            category="synthetic",
            visibility="public" if n != 2 else "private",
        )
        for n in range(1, 4)
    ]
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(entries))
    return path


def test_loader_public_ids_titles_pillars_and_hidden_metadata(tmp_path):
    documents = sources.load_corpus_documents(corpus(tmp_path))
    assert [document.id_ for document in documents] == ["fixture-001", "fixture-003"]
    document = documents[0]
    assert document.text == "Which fixture 1?\n\nFixture 1."
    assert document.metadata["title"] == "Which fixture 1?"
    assert document.metadata["type"] == "fixture"
    assert document.metadata["category"] == "synthetic"
    assert document.metadata["path"] == document.id_
    for mode in (MetadataMode.EMBED, MetadataMode.LLM):
        text = document.get_content(metadata_mode=mode)
        assert "fixture-001" not in text
        assert "synthetic" not in text
        assert "content_hash" not in text


def test_loader_structural_errors_are_content_safe(tmp_path):
    path = tmp_path / "corpus.json"
    for content in ('{"private text":', '{"private text": "draft"}'):
        path.write_text(content)
        with pytest.raises(ValueError) as error:
            sources.load_corpus_documents(path)
        assert "private text" not in str(error.value)


def test_loader_skips_bad_entry_without_logging_content(tmp_path, caplog):
    path = corpus(tmp_path)
    entries = json.loads(path.read_text())
    entries.append(dict(id="bad", question="sensitive fixture", visibility="public"))
    path.write_text(json.dumps(entries))
    assert len(sources.load_corpus_documents(path)) == 2
    assert "invalid_entry position=3" in caplog.text
    assert "sensitive fixture" not in caplog.text


@pytest.mark.parametrize("collection", ["brain", "about_aleix"])
def test_collection_load_and_store_routing(monkeypatch, collection):
    settings = Settings(_env_file=None, active_collection=collection)
    monkeypatch.setattr(sources, "load_brain_documents", lambda *args: "brain")
    monkeypatch.setattr(sources, "load_corpus_documents", lambda *args: "about_aleix")
    assert sources.load_documents(settings) == collection
    tables = []

    class Store:
        @classmethod
        def from_params(cls, **kwargs):
            tables.append(kwargs["table_name"])
            return object()

    monkeypatch.setattr(index, "PGVectorStore", Store)
    monkeypatch.setattr(index, "PostgresKVStore", Store)
    monkeypatch.setattr(index, "PostgresDocumentStore", lambda store: store)
    index.build_stores(settings, 8)
    assert tables == [f"{collection}_chunks", f"{collection}_docs"]


@pytest.mark.parametrize("collection", ["brain", "about_aleix"])
def test_query_and_answer_pass_collection_to_stores(monkeypatch, collection):
    settings = Settings(_env_file=None, active_collection=collection)
    calls = []
    embedding = SimpleNamespace(get_query_embedding=lambda question: [1.0])
    monkeypatch.setattr("homelab.api.app.get_settings", lambda: settings)
    monkeypatch.setattr(
        "homelab.api.app.load_routes",
        lambda **kw: SimpleNamespace(embedding_model=lambda purpose: embedding),
    )

    def stores(settings, dimension):
        calls.append(settings.active_collection)
        return object(), object()

    monkeypatch.setattr("homelab.api.app.build_stores", stores)
    monkeypatch.setattr("homelab.api.app._retrieve_with_embedding", lambda *a, **kw: [])
    monkeypatch.setattr(
        "homelab.api.app.answer_from_chunks",
        lambda *a: KnowledgeAnswerResponse(
            answer="No evidence.", refused=True, partial=False, sources=[]
        ),
    )
    for endpoint in ("query", "answer"):
        assert (
            TestClient(app)
            .post(f"/v1/knowledge/{endpoint}", json={"question": "Fixture?"})
            .status_code
            == 200
        )
    assert calls == [collection, collection]


@pytest.mark.parametrize("collection", ["brain", "about_aleix"])
def test_ingest_job_and_cli_follow_collection(monkeypatch, collection):
    from homelab import cli
    from homelab.jobs import tasks

    settings = Settings(_env_file=None, active_collection=collection)
    seen = []
    result = index.IngestionResult(1, 1, 0, 0)
    for module in (cli, tasks):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(
            module, "load_documents", lambda s: seen.append(s.active_collection) or []
        )
        monkeypatch.setattr(
            module, "load_routes", lambda **kw: SimpleNamespace(embedding_model=lambda p: object())
        )
        monkeypatch.setattr(module, "probe_embedding_dimension", lambda e: 8)
        monkeypatch.setattr(module, "build_stores", lambda s, d: (object(), object()))
        monkeypatch.setattr(module, "ingest", lambda *a, **kw: result)
    assert tasks.ingest_brain() == dict(documents_seen=1, nodes_written=1, skipped=0, deleted=0)
    assert cli.main(["ingest"]) == 0
    assert seen == [collection, collection]


def test_eval_arithmetic_and_refusals(tmp_path):
    documents = sources.load_corpus_documents(corpus(tmp_path))
    calls = []

    def retriever(question, top_k):
        assert top_k == 5
        calls.append(question)
        ids = ["other", "fixture-001"] if question == documents[0].metadata["title"] else ["other"]
        return [SimpleNamespace(path=entry_id) for entry_id in ids]

    result = evaluate(
        documents,
        retriever=retriever,
        answerer=lambda q, k: SimpleNamespace(refused=q == documents[1].metadata["title"]),
    )
    assert result.pop("error_count") == 0
    assert result.pop("failures") == []
    assert result.pop("answer_latency_seconds")["count"] == 2
    assert result == dict(
        documents=2,
        **{"hit@5": 0.5, "MRR": 0.25},
        miss_ids=["fixture-003"],
        refusal_count=1,
        refusal_ids=["fixture-003"],
    )
    assert len(calls) == 2
    assert evaluate([], retriever=retriever, answerer=lambda *a: None)["MRR"] == 0


def test_default_and_invalid_collection():
    assert Settings(_env_file=None).active_collection == "about_aleix"
    with pytest.raises(ValueError):
        Settings(_env_file=None, active_collection="unknown")


def test_corpus_pipeline_incremental_and_provenance(tmp_path):
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.storage.docstore import SimpleDocumentStore
    from llama_index.core.vector_stores import SimpleVectorStore

    documents = sources.load_corpus_documents(corpus(tmp_path))
    vector_store = SimpleVectorStore()
    docstore = SimpleDocumentStore()
    kwargs = dict(
        vector_store=vector_store, docstore=docstore, embed_model=MockEmbedding(embed_dim=8)
    )
    assert index.ingest(documents, **kwargs).nodes_written == 2
    assert index.ingest(documents, **kwargs).skipped == 2
    assert {metadata["path"] for metadata in vector_store._data.metadata_dict.values()} == {
        "fixture-001",
        "fixture-003",
    }


def test_eval_command_output_has_only_metrics_and_ids(tmp_path, monkeypatch, capsys):
    from homelab import cli
    from homelab.knowledge import evaluate as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: Settings(
            _env_file=None, corpus_file=corpus(tmp_path), eval_file=tmp_path / "missing.json"
        ),
    )
    monkeypatch.setattr("homelab.api.app.query_brain", lambda *a: [])
    monkeypatch.setattr(
        "homelab.api.app.answer_knowledge", lambda body: SimpleNamespace(refused=True)
    )
    assert cli.main(["eval"]) == 0
    output = capsys.readouterr().out
    assert "Which fixture" not in output
    assert json.loads(output)["refusal_count"] == 2


async def test_ingest_endpoint_enqueues_selected_collection_job(monkeypatch):
    from homelab.api.app import trigger_brain_ingest
    from homelab.jobs.tasks import ingest_brain

    async def defer():
        return 99

    monkeypatch.setattr(ingest_brain, "defer_async", defer)
    assert await trigger_brain_ingest() == {"job_id": 99}

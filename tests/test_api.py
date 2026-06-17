from __future__ import annotations

from fastapi.testclient import TestClient

from arxiv_meta import server


def client_for(db_path):
    server.configure_engine(str(db_path))
    return TestClient(server.app)


def test_health_stats_and_arxiv_lookup(sample_db):
    client = client_for(sample_db)

    health = client.get("/health")
    stats = client.get("/stats")
    found = client.get("/arxiv/0704.0001")
    missing = client.get("/arxiv/9999.99999")

    assert health.status_code == 200
    assert health.json()["db_ready"] is True
    assert health.json()["papers"] == 5
    assert stats.status_code == 200
    assert stats.json()["total_papers"] == 5
    assert found.status_code == 200
    assert found.json()["update_date"] == "2007-05-23"
    assert found.json()["categories"] == ["cs.CL", "cs.AI"]
    assert missing.status_code == 404


def test_search_api_filters(sample_db):
    client = client_for(sample_db)

    response = client.get(
        "/search",
        params={
            "q": "Taxonomy Metadata",
            "cat": "cs.CL",
            "update_date_from": "2024-01-01",
            "update_date_to": "2024-01-31",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["arxiv_id"] == "2401.00001"
    assert "update_date" in body["results"][0]


def test_search_api_accepts_pasted_title_punctuation(sample_db):
    client = client_for(sample_db)

    response = client.get(
        "/search",
        params={"q": 'Punctuation: Title - Boundary (Quoted "Example")'},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["arxiv_id"] == "2502.00001"


def test_candidate_search_api_returns_review_evidence(sample_db):
    client = client_for(sample_db)

    response = client.get(
        "/candidate-search",
        params={"q": "Taxonomy Metadata Unmatched", "limit": 3},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "candidate"
    assert body["auto_accept"] is False
    assert body["results"][0]["source_id"] == "2401.00001"
    assert body["results"][0]["review_candidate"] is True
    assert body["results"][0]["auto_accept"] is False
    assert "taxonomy" in body["results"][0]["shared_tokens"]
    assert "metadata" in body["results"][0]["shared_tokens"]


def test_candidate_search_api_filters(sample_db):
    client = client_for(sample_db)

    response = client.get(
        "/candidate-search",
        params={
            "q": "Punctuation Example Missing",
            "cat": "cs.CL",
            "update_date_from": "2025-01-01",
            "update_date_to": "2025-12-31",
        },
    )

    assert response.status_code == 200
    assert [row["source_id"] for row in response.json()["results"]] == ["2502.00001"]


def test_candidates_batch_api_returns_review_candidates(sample_db):
    client = client_for(sample_db)

    response = client.post(
        "/candidates-batch",
        json={
            "items": [
                {"request_id": "a", "query": "Taxonomy Metadata Unmatched", "limit": 3},
                {
                    "request_id": "b",
                    "query": "Punctuation Example Missing",
                    "limit": 3,
                    "categories": ["cs.CL"],
                },
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "candidate_batch"
    assert body["auto_accept"] is False
    assert body["total"] == 2
    assert body["results"][0]["request_id"] == "a"
    assert body["results"][0]["results"][0]["source_id"] == "2401.00001"
    assert body["results"][1]["request_id"] == "b"
    assert body["results"][1]["results"][0]["source_id"] == "2502.00001"
    assert all(
        candidate["auto_accept"] is False
        for item in body["results"]
        for candidate in item["results"]
    )


def test_batch_doi_api(sample_db):
    client = client_for(sample_db)

    response = client.post(
        "/batch-doi",
        json={"dois": ["10.1000/alpha", "10.1000/missing"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": {"10.1000/alpha": "0704.0001"},
        "not_found": ["10.1000/missing"],
    }


def test_api_rejects_invalid_fts_query_with_400(sample_db):
    client = client_for(sample_db)

    response = client.get("/search", params={"q": "***"})
    advanced = client.get("/search", params={"q": "title:Quantum"})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_fts_query"
    assert advanced.status_code == 400
    assert advanced.json()["error"] == "invalid_fts_query"


def test_api_rejects_query_and_request_limits(sample_db):
    client = client_for(sample_db)

    long_query = client.get("/search", params={"q": "x" * 257})
    large_limit = client.get("/search", params={"q": "Quantum", "limit": 501})
    large_batch = client.post("/batch-doi", json={"dois": ["10.1000/x"] * 501})
    long_doi = client.post("/batch-doi", json={"dois": ["1" * 257]})

    assert long_query.status_code == 400
    assert long_query.json()["error"] == "request_too_large"
    assert large_limit.status_code == 400
    assert large_limit.json()["error"] == "request_too_large"
    assert large_batch.status_code == 400
    assert large_batch.json()["error"] == "request_too_large"
    assert long_doi.status_code == 400
    assert long_doi.json()["error"] == "request_too_large"


def test_api_rejects_invalid_date(sample_db):
    client = client_for(sample_db)

    response = client.get("/search", params={"q": "Quantum", "update_date_from": "2024-99-01"})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_date"

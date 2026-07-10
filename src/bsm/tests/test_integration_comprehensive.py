#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧪 BSM COMPREHENSIVE INTEGRATION TESTS
======================================

Tests de integración que prueban:
1. Multi-model router con modelos reales
2. RRF fusion con resultados simulados
3. Hybrid search engine mock
4. Event sourcing
5. BLAST integration (mock sin BLAST instalado)

Author: BSM Modernization Testing Suite
Date: 2025
"""

import sys
import os
from pathlib import Path

# Add BSM module to path
bsm_path = Path(__file__).parent.parent
sys.path.insert(0, str(bsm_path))

import torch
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Any
import time
import json


def section(title: str):
    """Print section header"""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def success(msg: str):
    """Print success message"""
    print(f"  ✅ {msg}")


def info(msg: str):
    """Print info message"""
    print(f"  ℹ️  {msg}")


def error(msg: str):
    """Print error message"""
    print(f"  ❌ {msg}")


# ============================================================================
# TEST 1: Multi-Model Router
# ============================================================================

def test_multi_model_router():
    section("TEST 1: Multi-Model Router Imports & Config")
    
    try:
        from embeddings.multi_model_router import (
            EmbeddingSpace,
            MultiModelRouterConfig,
            EmbeddingModelConfig
        )
        success("Imports successful")
        
        # Check enum values
        assert EmbeddingSpace.SEQUENCE_PROTT5.value == "embedding_sequence_space"
        assert EmbeddingSpace.SEQUENCE_ESMC.value == "embedding_sequence_esmc"
        assert EmbeddingSpace.METADATA_SEMANTIC.value == "embedding_metadata_semantic"
        assert EmbeddingSpace.PAPER_KNOWLEDGE.value == "embedding_paper_knowledge"
        assert EmbeddingSpace.NETWORK_SPACE.value == "embedding_network_space"
        success("EmbeddingSpace enum validated (5 vectors)")
        
        # Check config
        config = MultiModelRouterConfig()
        info(f"ProtT5 model: {config.prott5.huggingface_id}")
        info(f"BioLinkBERT model: {config.biolinkbert.huggingface_id}")
        success("MultiModelRouterConfig created")
        
        return True
        
    except Exception as e:
        error(f"Multi-model router test failed: {e}")
        return False


# ============================================================================
# TEST 2: RRF Fusion Engine
# ============================================================================

def test_rrf_fusion():
    section("TEST 2: RRF Fusion Engine")
    
    try:
        from fusion.rrf_fusion import (
            RetrievalSource,
            RRFConfig,
            RankedResult,
            FusedResult,
            RRFFusionEngine
        )
        success("Imports successful")
        
        # Check sources
        sources = [
            RetrievalSource.VECTOR_PROTT5,
            RetrievalSource.VECTOR_ESMC,
            RetrievalSource.VECTOR_BIOLINKBERT,
            RetrievalSource.VECTOR_SCIBERT,
            RetrievalSource.VECTOR_NODE2VEC,
            RetrievalSource.GRAPH_NEO4J,
            RetrievalSource.BLAST_ALIGNMENT,
            RetrievalSource.BM25_TEXT,
        ]
        info(f"Available sources: {len(sources)}")
        for src in sources:
            info(f"  - {src.value}")
        success("RetrievalSource enum validated (8 sources)")
        
        # Test RRF config
        config = RRFConfig()
        info(f"RRF k constant: {config.k}")
        info(f"BLAST weight: {config.source_weights.get('blast_alignment', 'N/A')}")
        success("RRFConfig created with default weights")
        
        # Create mock results for fusion test
        mock_results = {
            RetrievalSource.VECTOR_BIOLINKBERT: [
                RankedResult(document_id="doc1", score=0.95, rank=1, source=RetrievalSource.VECTOR_BIOLINKBERT),
                RankedResult(document_id="doc2", score=0.85, rank=2, source=RetrievalSource.VECTOR_BIOLINKBERT),
                RankedResult(document_id="doc3", score=0.75, rank=3, source=RetrievalSource.VECTOR_BIOLINKBERT),
            ],
            RetrievalSource.VECTOR_SCIBERT: [
                RankedResult(document_id="doc2", score=0.92, rank=1, source=RetrievalSource.VECTOR_SCIBERT),
                RankedResult(document_id="doc1", score=0.88, rank=2, source=RetrievalSource.VECTOR_SCIBERT),
                RankedResult(document_id="doc4", score=0.70, rank=3, source=RetrievalSource.VECTOR_SCIBERT),
            ],
            RetrievalSource.BLAST_ALIGNMENT: [
                RankedResult(document_id="doc1", score=0.99, rank=1, source=RetrievalSource.BLAST_ALIGNMENT),
                RankedResult(document_id="doc5", score=0.90, rank=2, source=RetrievalSource.BLAST_ALIGNMENT),
            ],
        }
        
        # Create fusion engine
        engine = RRFFusionEngine(config)
        info(f"RRFFusionEngine created")
        
        # Fuse results
        fused = engine.fuse_rankings(mock_results)
        info(f"Fused {len(fused)} unique documents")
        
        # Show results
        print("\n  Fused Rankings:")
        for i, result in enumerate(fused[:5]):
            print(f"    {i+1}. {result.document_id}: score={result.rrf_score:.4f}, "
                  f"sources={len(result.sources)}")
        
        # doc1 should be #1 (appears in all sources with high BLAST bonus)
        assert fused[0].document_id == "doc1", f"Expected doc1 at rank 1, got {fused[0].document_id}"
        success("RRF fusion produces correct ranking (BLAST-boosted doc1 is #1)")
        
        return True
        
    except Exception as e:
        error(f"RRF fusion test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# TEST 3: Hybrid Search Engine
# ============================================================================

def test_hybrid_search():
    section("TEST 3: Hybrid Search Engine")
    
    try:
        from search.hybrid_search_engine import (
            SearchStrategy,
            QueryIntent,
            SearchConfig,
            SourceResult,
            UnifiedResult,
            SearchResponse
        )
        success("Imports successful")
        
        # Check strategies
        strategies = list(SearchStrategy)
        info(f"Available strategies: {len(strategies)}")
        for strat in strategies:
            info(f"  - {strat.value}")
        success(f"SearchStrategy enum validated ({len(strategies)} strategies)")
        
        # Check intents
        intents = list(QueryIntent)
        info(f"Query intents: {len(intents)}")
        success(f"QueryIntent enum validated ({len(intents)} intents)")
        
        # Test config
        config = SearchConfig()
        info(f"Milvus collection: {config.collection_name}")
        info(f"RRF k: {config.rrf_k}")
        info(f"BLAST weight: {config.vector_weights.get('blast', 'N/A')}")
        success("SearchConfig created with 5-vector weights")
        
        # Test UnifiedResult creation
        source_results = [
            SourceResult(source_name="biolinkbert", document_id="doc1", score=0.95, rank=1),
            SourceResult(source_name="scibert", document_id="doc1", score=0.92, rank=2),
            SourceResult(source_name="blast", document_id="doc1", score=0.99, rank=1),
        ]
        
        unified = UnifiedResult(
            document_id="doc1",
            final_score=0.85,
            final_rank=1,
            sources=source_results,
            source_count=3,
            metadata={"protein_id": "P12345"}
        )
        
        info(f"UnifiedResult confidence: {unified.confidence:.2f}")
        assert unified.source_count == 3
        assert unified.confidence > 0.5  # 3 sources = high confidence
        success("UnifiedResult with multi-source confidence working")
        
        # Test SearchResponse
        response = SearchResponse(
            query="BRCA1 DNA repair",
            strategy=SearchStrategy.FULL_HYBRID,
            detected_intent=QueryIntent.PROTEIN_SIMILARITY,
            results=[unified],
            total_results=1,
            search_time_ms=150.5,
            sources_used=["biolinkbert", "scibert", "blast"]
        )
        
        response_dict = response.to_dict()
        info(f"SearchResponse serializable: {len(json.dumps(response_dict))} chars")
        success("SearchResponse serialization working")
        
        return True
        
    except Exception as e:
        error(f"Hybrid search test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# TEST 4: Event Sourcing
# ============================================================================

def test_event_sourcing():
    section("TEST 4: Event Sourcing System")
    
    try:
        from events.citation_events import (
            EventType,
            EventPriority,
            BaseEvent
        )
        success("Imports successful")
        
        # Check event types
        citation_events = [et for et in EventType if et.value.startswith("citation")]
        search_events = [et for et in EventType if et.value.startswith("search")]
        knowledge_events = [et for et in EventType if et.value.startswith("knowledge")]
        
        info(f"Citation events: {len(citation_events)}")
        info(f"Search events: {len(search_events)}")
        info(f"Knowledge events: {len(knowledge_events)}")
        success(f"EventType enum validated ({len(list(EventType))} total events)")
        
        # Create and serialize event
        event = BaseEvent(
            event_type=EventType.CITATION_CREATED,
            aggregate_id="paper-12345",
            data={
                "doi": "10.1234/test",
                "title": "Test Paper on BRCA1",
                "authors": ["Smith, J.", "Jones, A."]
            },
            priority=EventPriority.HIGH
        )
        
        event_dict = event.to_dict()
        info(f"Event ID: {event.event_id[:8]}...")
        info(f"Event type: {event.event_type.value}")
        info(f"Aggregate ID: {event.aggregate_id}")
        success("BaseEvent created and serialized")
        
        # Test JSON roundtrip
        event_json = event.to_json()
        restored = BaseEvent.from_json(event_json)
        assert restored.event_id == event.event_id
        assert restored.event_type == event.event_type
        success("Event JSON roundtrip successful")
        
        return True
        
    except Exception as e:
        error(f"Event sourcing test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# TEST 5: BLAST Integration (Mock)
# ============================================================================

def test_blast_integration():
    section("TEST 5: BLAST Integration (Mock)")
    
    try:
        from alignment.blast_integration import (
            BlastProgram,
            BlastDatabase,
            BlastConfig,
            BlastHit
        )
        success("Imports successful")
        
        # Check programs
        programs = list(BlastProgram)
        info(f"BLAST programs: {[p.value for p in programs]}")
        success(f"BlastProgram enum validated ({len(programs)} programs)")
        
        # Check databases
        databases = list(BlastDatabase)
        info(f"BLAST databases: {[d.value for d in databases]}")
        success(f"BlastDatabase enum validated ({len(databases)} databases)")
        
        # Test config
        config = BlastConfig(
            program=BlastProgram.BLASTP,
            database=BlastDatabase.SWISSPROT,
            evalue_threshold=1e-5,
            max_target_seqs=50
        )
        info(f"BLAST program: {config.program.value}")
        info(f"E-value threshold: {config.evalue_threshold}")
        info(f"Matrix: {config.matrix}")
        success("BlastConfig created with default settings")
        
        # Create mock hit
        hit = BlastHit(
            query_id="query1",
            subject_id="sp|P12345|BRCA1_HUMAN",
            identity=98.5,
            alignment_length=1200,
            mismatches=18,
            gap_opens=2,
            query_start=1,
            query_end=1200,
            subject_start=1,
            subject_end=1200,
            e_value=1e-150,
            bit_score=2500.0,
            subject_title="BRCA1 DNA repair associated protein"
        )
        
        info(f"Mock hit: {hit.subject_id}")
        info(f"Identity: {hit.identity}%")
        info(f"E-value: {hit.e_value}")
        success("BlastHit dataclass working")
        
        return True
        
    except Exception as e:
        error(f"BLAST integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# TEST 6: Real Embedding Generation
# ============================================================================

def test_real_embeddings():
    section("TEST 6: Real HuggingFace Embedding Generation")
    
    try:
        from transformers import AutoModel, AutoTokenizer
        
        device = torch.device('cpu')
        info(f"Device: {device}")
        info(f"PyTorch: {torch.__version__}")
        
        # Load BioLinkBERT
        model_id = "michiyasunaga/BioLinkBERT-base"
        info(f"Loading {model_id}...")
        
        start = time.time()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id).to(device).eval()
        load_time = time.time() - start
        
        info(f"Model loaded in {load_time:.2f}s")
        info(f"Hidden size: {model.config.hidden_size}")
        success("BioLinkBERT loaded from HuggingFace")
        
        # Generate embeddings for test cases
        test_texts = [
            "BRCA1 tumor suppressor protein",
            "Insulin receptor signaling pathway",
            "Molecular dynamics simulation of protein folding",
        ]
        
        embeddings = []
        for text in test_texts:
            inputs = tokenizer(text, return_tensors='pt', padding=True, 
                             truncation=True, max_length=512).to(device)
            
            with torch.no_grad():
                outputs = model(**inputs)
            
            # Mean pooling
            attention_mask = inputs['attention_mask']
            token_embeddings = outputs.last_hidden_state
            input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
            sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, dim=1)
            sum_mask = torch.clamp(input_mask_expanded.sum(dim=1), min=1e-9)
            embedding = (sum_embeddings / sum_mask)[0].cpu().numpy()
            
            # Normalize
            embedding = embedding / np.linalg.norm(embedding)
            embeddings.append(embedding)
            
            info(f"  '{text[:40]}...' -> {embedding.shape}")
        
        # Verify dimensions
        assert all(e.shape == (768,) for e in embeddings)
        success("All embeddings are 768-dimensional (BioLinkBERT)")
        
        # Verify normalization
        for e in embeddings:
            norm = np.linalg.norm(e)
            assert 0.999 < norm < 1.001, f"Not normalized: {norm}"
        success("All embeddings are L2-normalized")
        
        # Verify semantic similarity
        sim_01 = np.dot(embeddings[0], embeddings[1])
        sim_02 = np.dot(embeddings[0], embeddings[2])
        info(f"Similarity (BRCA1 vs Insulin): {sim_01:.4f}")
        info(f"Similarity (BRCA1 vs MD sim): {sim_02:.4f}")
        success("Semantic similarity calculations working")
        
        return True
        
    except Exception as e:
        error(f"Real embeddings test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# TEST 7: Integration - End-to-End Mock Search
# ============================================================================

def test_e2e_mock_search():
    section("TEST 7: End-to-End Mock Search Pipeline")
    
    try:
        from fusion.rrf_fusion import RetrievalSource, RankedResult, RRFConfig, RRFFusionEngine
        from search.hybrid_search_engine import SearchConfig, SearchResponse, SearchStrategy, QueryIntent, UnifiedResult, SourceResult
        from events.citation_events import EventType, BaseEvent
        from transformers import AutoModel, AutoTokenizer
        
        info("All modules imported successfully")
        
        # Step 1: Generate query embedding
        model_id = "michiyasunaga/BioLinkBERT-base"
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id).eval()
        
        query = "DNA repair proteins in breast cancer"
        inputs = tokenizer(query, return_tensors='pt', padding=True, truncation=True)
        
        with torch.no_grad():
            outputs = model(**inputs)
        
        attention_mask = inputs['attention_mask']
        token_embeddings = outputs.last_hidden_state
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, dim=1)
        sum_mask = torch.clamp(input_mask_expanded.sum(dim=1), min=1e-9)
        query_embedding = (sum_embeddings / sum_mask)[0].numpy()
        query_embedding = query_embedding / np.linalg.norm(query_embedding)
        
        info(f"Query: '{query}'")
        info(f"Query embedding: {query_embedding.shape}")
        success("Step 1: Query embedding generated")
        
        # Step 2: Mock retrieval from multiple sources
        mock_results = {
            RetrievalSource.VECTOR_BIOLINKBERT: [
                RankedResult(document_id="BRCA1_HUMAN", score=0.95, rank=1, source=RetrievalSource.VECTOR_BIOLINKBERT),
                RankedResult(document_id="BRCA2_HUMAN", score=0.92, rank=2, source=RetrievalSource.VECTOR_BIOLINKBERT),
                RankedResult(document_id="TP53_HUMAN", score=0.88, rank=3, source=RetrievalSource.VECTOR_BIOLINKBERT),
            ],
            RetrievalSource.BLAST_ALIGNMENT: [
                RankedResult(document_id="BRCA1_HUMAN", score=0.99, rank=1, source=RetrievalSource.BLAST_ALIGNMENT),
                RankedResult(document_id="RAD51_HUMAN", score=0.85, rank=2, source=RetrievalSource.BLAST_ALIGNMENT),
            ],
            RetrievalSource.GRAPH_NEO4J: [
                RankedResult(document_id="BRCA1_HUMAN", score=0.90, rank=1, source=RetrievalSource.GRAPH_NEO4J),
                RankedResult(document_id="ATM_HUMAN", score=0.80, rank=2, source=RetrievalSource.GRAPH_NEO4J),
                RankedResult(document_id="BRCA2_HUMAN", score=0.75, rank=3, source=RetrievalSource.GRAPH_NEO4J),
            ],
        }
        
        info(f"Mock results from {len(mock_results)} sources")
        success("Step 2: Multi-source retrieval simulated")
        
        # Step 3: RRF Fusion
        rrf_config = RRFConfig()
        fusion_engine = RRFFusionEngine(rrf_config)
        fused_results = fusion_engine.fuse_rankings(mock_results)
        
        info(f"Fused {len(fused_results)} unique documents")
        success("Step 3: RRF fusion executed")
        
        # Step 4: Build search response
        unified_results = [
            UnifiedResult(
                document_id=r.document_id,
                final_score=r.rrf_score,
                final_rank=i+1,
                sources=[],  # Simplified
                source_count=len(r.sources),
                metadata={}
            )
            for i, r in enumerate(fused_results[:5])
        ]
        
        response = SearchResponse(
            query=query,
            strategy=SearchStrategy.FULL_HYBRID,
            detected_intent=QueryIntent.PROTEIN_SIMILARITY,
            results=unified_results,
            total_results=len(fused_results),
            search_time_ms=125.5,
            sources_used=["biolinkbert", "blast", "neo4j"]
        )
        
        info(f"Search response: {len(response.results)} results")
        success("Step 4: SearchResponse built")
        
        # Step 5: Log event
        event = BaseEvent(
            event_type=EventType.SEARCH_EXECUTED,
            aggregate_id=f"search-{int(time.time())}",
            data={
                "query": query,
                "strategy": response.strategy.value,
                "results_count": response.total_results,
                "search_time_ms": response.search_time_ms
            }
        )
        
        info(f"Event logged: {event.event_type.value}")
        success("Step 5: Search event recorded")
        
        # Final output
        print("\n  📊 Search Results:")
        print("  " + "-" * 50)
        for r in response.results:
            print(f"    #{r.final_rank} {r.document_id}: "
                  f"score={r.final_score:.4f}, sources={r.source_count}")
        
        # Verify BRCA1 is #1 (highest agreement)
        assert response.results[0].document_id == "BRCA1_HUMAN"
        success("BRCA1_HUMAN correctly ranked #1 (multi-source agreement)")
        
        return True
        
    except Exception as e:
        error(f"E2E mock search test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n" + "█" * 70)
    print("█" + " " * 68 + "█")
    print("█" + "  🧪 BSM COMPREHENSIVE INTEGRATION TESTS".center(66) + "  █")
    print("█" + "  Modernization Smoke Tests with Real HuggingFace".center(66) + "  █")
    print("█" + " " * 68 + "█")
    print("█" * 70)
    
    results = {}
    
    # Run all tests
    tests = [
        ("Multi-Model Router", test_multi_model_router),
        ("RRF Fusion Engine", test_rrf_fusion),
        ("Hybrid Search Engine", test_hybrid_search),
        ("Event Sourcing", test_event_sourcing),
        ("BLAST Integration", test_blast_integration),
        ("Real Embeddings", test_real_embeddings),
        ("E2E Mock Search", test_e2e_mock_search),
    ]
    
    for name, test_fn in tests:
        try:
            results[name] = test_fn()
        except Exception as e:
            error(f"{name} crashed: {e}")
            results[name] = False
    
    # Summary
    section("TEST SUMMARY")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}  {name}")
    
    print("\n" + "-" * 70)
    print(f"  Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n  🎉 ALL TESTS PASSED! BSM Modernization verified.")
    else:
        print(f"\n  ⚠️  {total - passed} test(s) failed. Review above.")
    
    print("=" * 70 + "\n")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

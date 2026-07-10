#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧪 BSM SMOKE TESTS - REAL HUGGINGFACE MODEL INTEGRATION
========================================================

Tests de humo que verifican:
1. Carga real de modelos de HuggingFace
2. Generación de embeddings reales
3. Dimensiones correctas de vectores
4. Integración end-to-end

Modelos probados:
- BioLinkBERT: michiyasunaga/BioLinkBERT-base (768D)
- SciBERT: allenai/scibert_scivocab_uncased (768D)  
- PubMedBERT: microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract (768D)
- ProtT5 (simulado por recursos): Rostlab/prot_t5_xl_uniref50 (1024D)

Author: BSM Modernization Testing Suite
Date: 2025
"""

import pytest
import sys
import time
import torch
import numpy as np
from typing import Dict, Any, Tuple
from dataclasses import dataclass
import logging

# Configure logging for tests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# TEST CONFIGURATION
# ============================================================================

@dataclass
class ModelTestConfig:
    """Configuración para test de modelo"""
    model_id: str
    expected_dim: int
    test_input: str
    model_type: str  # "bert", "t5", "esm"
    max_length: int = 512


# Modelos a probar (ordenados por tamaño ascendente)
MODELS_TO_TEST = [
    ModelTestConfig(
        model_id="michiyasunaga/BioLinkBERT-base",
        expected_dim=768,
        test_input="BRCA1 is a protein involved in DNA repair and tumor suppression.",
        model_type="bert",
        max_length=512
    ),
    ModelTestConfig(
        model_id="allenai/scibert_scivocab_uncased",
        expected_dim=768,
        test_input="The protein kinase phosphorylates serine residues.",
        model_type="bert",
        max_length=512
    ),
    ModelTestConfig(
        model_id="microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract",
        expected_dim=768,
        test_input="Molecular dynamics simulation reveals protein folding mechanism.",
        model_type="bert",
        max_length=512
    ),
]

# Secuencias de prueba para modelos de proteínas
TEST_PROTEIN_SEQUENCES = [
    # BRCA1 fragment (50 aa)
    "MDLSALRVEEVQNVINAMQKILECPICLELIKEPVSTKCDHIFCKFCML",
    # P53 fragment (40 aa)
    "MEEPQSDPSVEPPLSQETFSDLWKLLPENNVLSPLPSQAMDDL",
    # Insulin fragment (30 aa)
    "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHL",
]


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture(scope="module")
def device():
    """Determina el device disponible"""
    if torch.cuda.is_available():
        return torch.device("cuda")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


@pytest.fixture(scope="module")
def transformers_available():
    """Verifica si transformers está disponible"""
    try:
        import transformers
        return True
    except ImportError:
        return False


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def load_bert_model(model_id: str, device: torch.device) -> Tuple[Any, Any]:
    """Carga un modelo BERT y su tokenizer"""
    from transformers import AutoModel, AutoTokenizer
    
    logger.info(f"📥 Loading model: {model_id}")
    start_time = time.time()
    
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id)
    model.to(device)
    model.eval()
    
    load_time = time.time() - start_time
    logger.info(f"✅ Model loaded in {load_time:.2f}s")
    
    return model, tokenizer


def generate_bert_embedding(
    model, 
    tokenizer, 
    text: str, 
    device: torch.device,
    max_length: int = 512
) -> np.ndarray:
    """Genera embedding usando modelo BERT-like"""
    
    # Tokenizar
    inputs = tokenizer(
        text,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=max_length
    ).to(device)
    
    # Inferencia sin gradientes
    with torch.no_grad():
        outputs = model(**inputs)
    
    # Mean pooling sobre la secuencia
    # outputs.last_hidden_state: [batch, seq_len, hidden_dim]
    attention_mask = inputs["attention_mask"]
    token_embeddings = outputs.last_hidden_state
    
    # Expandir mask para multiplicación
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    
    # Mean pooling
    sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, dim=1)
    sum_mask = torch.clamp(input_mask_expanded.sum(dim=1), min=1e-9)
    mean_pooled = sum_embeddings / sum_mask
    
    # Normalizar L2
    embedding = mean_pooled[0].cpu().numpy()
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm
    
    return embedding


# ============================================================================
# SMOKE TESTS - HUGGINGFACE MODELS
# ============================================================================

class TestHuggingFaceSmoke:
    """Tests de humo para modelos de HuggingFace"""
    
    def test_transformers_import(self):
        """Test que transformers puede importarse"""
        try:
            import transformers
            from transformers import AutoModel, AutoTokenizer
            assert hasattr(transformers, "__version__")
            logger.info(f"✅ Transformers version: {transformers.__version__}")
        except ImportError as e:
            pytest.fail(f"Cannot import transformers: {e}")
    
    def test_torch_available(self, device):
        """Test que PyTorch está disponible"""
        logger.info(f"✅ PyTorch device: {device}")
        logger.info(f"✅ PyTorch version: {torch.__version__}")
        
        # Crear tensor de prueba
        x = torch.randn(10, 768, device=device)
        assert x.shape == (10, 768)
    
    @pytest.mark.parametrize("config", MODELS_TO_TEST)
    def test_model_loading(self, config: ModelTestConfig, device):
        """Test que cada modelo puede cargarse"""
        try:
            model, tokenizer = load_bert_model(config.model_id, device)
            
            # Verificar que el modelo se cargó
            assert model is not None
            assert tokenizer is not None
            
            # Verificar tipo
            assert hasattr(model, "config")
            assert model.config.hidden_size == config.expected_dim
            
            logger.info(f"✅ {config.model_id}: hidden_size={model.config.hidden_size}")
            
        except Exception as e:
            pytest.fail(f"Failed to load {config.model_id}: {e}")
    
    @pytest.mark.parametrize("config", MODELS_TO_TEST)
    def test_embedding_generation(self, config: ModelTestConfig, device):
        """Test que cada modelo genera embeddings correctos"""
        try:
            model, tokenizer = load_bert_model(config.model_id, device)
            
            # Generar embedding
            embedding = generate_bert_embedding(
                model, tokenizer, config.test_input, device, config.max_length
            )
            
            # Verificar dimensiones
            assert embedding.shape == (config.expected_dim,), \
                f"Expected {config.expected_dim}, got {embedding.shape}"
            
            # Verificar que está normalizado (L2 norm ≈ 1)
            norm = np.linalg.norm(embedding)
            assert 0.99 < norm < 1.01, f"Expected normalized embedding, got norm={norm}"
            
            # Verificar que no es todo ceros o NaN
            assert not np.isnan(embedding).any(), "Embedding contains NaN"
            assert not np.all(embedding == 0), "Embedding is all zeros"
            
            logger.info(f"✅ {config.model_id}: embedding shape={embedding.shape}, norm={norm:.4f}")
            
        except Exception as e:
            pytest.fail(f"Failed to generate embedding with {config.model_id}: {e}")
    
    def test_biolinkbert_detailed(self, device):
        """Test detallado de BioLinkBERT (modelo principal para metadata)"""
        from transformers import AutoModel, AutoTokenizer
        
        model_id = "michiyasunaga/BioLinkBERT-base"
        
        # Cargar
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id).to(device).eval()
        
        # Tests con diferentes inputs
        test_cases = [
            "BRCA1 protein",
            "Kinase phosphorylation pathway",
            "DNA repair mechanism in cancer cells",
            "Protein-protein interaction network",
            "Molecular dynamics simulation of membrane proteins",
        ]
        
        embeddings = []
        for text in test_cases:
            emb = generate_bert_embedding(model, tokenizer, text, device)
            embeddings.append(emb)
            logger.info(f"  • '{text[:40]}...': dim={emb.shape[0]}")
        
        # Verificar que embeddings diferentes son realmente diferentes
        embeddings = np.array(embeddings)
        
        # Calcular similitudes
        for i in range(len(test_cases)):
            for j in range(i+1, len(test_cases)):
                sim = np.dot(embeddings[i], embeddings[j])
                logger.info(f"  Similarity [{i}][{j}]: {sim:.4f}")
                
                # Embeddings del mismo dominio deberían tener alguna similitud
                # pero no ser idénticos
                assert 0 < sim < 1.0, "Unexpected similarity value"
        
        logger.info(f"✅ BioLinkBERT detailed test passed")


# ============================================================================
# SMOKE TESTS - BSM MODULES
# ============================================================================

class TestBSMModulesSmoke:
    """Tests de humo para módulos BSM"""
    
    def test_import_multi_model_router(self):
        """Test import del multi-model router"""
        try:
            # Add path
            import sys
            sys.path.insert(0, str(__file__).replace("tests/test_smoke_huggingface_real.py", ""))
            
            from embeddings.multi_model_router import (
                EmbeddingSpace,
                MultiModelRouterConfig,
                EmbeddingModelConfig
            )
            
            # Verificar enums
            assert EmbeddingSpace.SEQUENCE_PROTT5.value == "embedding_sequence_space"
            assert EmbeddingSpace.METADATA_SEMANTIC.value == "embedding_metadata_semantic"
            
            logger.info("✅ Multi-model router imports successfully")
            
        except ImportError as e:
            pytest.fail(f"Cannot import multi_model_router: {e}")
    
    def test_import_rrf_fusion(self):
        """Test import del RRF fusion"""
        try:
            import sys
            sys.path.insert(0, str(__file__).replace("tests/test_smoke_huggingface_real.py", ""))
            
            from fusion.rrf_fusion import (
                RetrievalSource,
                RRFConfig,
                RankedResult,
                FusedResult
            )
            
            # Verificar fuentes
            assert RetrievalSource.BLAST_ALIGNMENT.value == "blast_alignment"
            assert RetrievalSource.GRAPH_NEO4J.value == "graph_neo4j"
            
            logger.info("✅ RRF fusion imports successfully")
            
        except ImportError as e:
            pytest.fail(f"Cannot import rrf_fusion: {e}")
    
    def test_import_hybrid_search(self):
        """Test import del hybrid search engine"""
        try:
            import sys
            sys.path.insert(0, str(__file__).replace("tests/test_smoke_huggingface_real.py", ""))
            
            from search.hybrid_search_engine import (
                SearchStrategy,
                QueryIntent,
                SearchConfig,
                SourceResult,
                UnifiedResult
            )
            
            # Verificar estrategias
            assert SearchStrategy.FULL_HYBRID.value == "full_hybrid"
            assert QueryIntent.PROTEIN_SIMILARITY.name == "PROTEIN_SIMILARITY"
            
            logger.info("✅ Hybrid search engine imports successfully")
            
        except ImportError as e:
            pytest.fail(f"Cannot import hybrid_search_engine: {e}")
    
    def test_import_blast_integration(self):
        """Test import del BLAST integration"""
        try:
            import sys
            sys.path.insert(0, str(__file__).replace("tests/test_smoke_huggingface_real.py", ""))
            
            from alignment.blast_integration import (
                BlastProgram,
                BlastDatabase,
                BlastConfig,
                BlastHit
            )
            
            # Verificar programas
            assert BlastProgram.BLASTP.value == "blastp"
            assert BlastDatabase.SWISSPROT.value == "swissprot"
            
            logger.info("✅ BLAST integration imports successfully")
            
        except ImportError as e:
            pytest.fail(f"Cannot import blast_integration: {e}")
    
    def test_import_citation_events(self):
        """Test import del event sourcing"""
        try:
            import sys
            sys.path.insert(0, str(__file__).replace("tests/test_smoke_huggingface_real.py", ""))
            
            from events.citation_events import (
                EventType,
                EventPriority,
                BaseEvent
            )
            
            # Verificar eventos
            assert EventType.CITATION_CREATED.value == "citation.created"
            assert EventPriority.CRITICAL.value == 3
            
            logger.info("✅ Citation events imports successfully")
            
        except ImportError as e:
            pytest.fail(f"Cannot import citation_events: {e}")


# ============================================================================
# INTEGRATION SMOKE TESTS
# ============================================================================

class TestIntegrationSmoke:
    """Tests de integración de humo"""
    
    def test_embedding_dimensions_match_schema(self, device):
        """Verifica que las dimensiones coinciden con el schema Milvus"""
        from transformers import AutoModel, AutoTokenizer
        
        # Schema esperado (de la documentación)
        expected_dims = {
            "embedding_sequence_space": 1024,      # ProtT5
            "embedding_sequence_esmc": 2560,       # ESM-C
            "embedding_metadata_semantic": 768,    # BioLinkBERT
            "embedding_paper_knowledge": 768,      # SciBERT
            "embedding_network_space": 512,        # node2vec
        }
        
        # Verificar modelos 768D
        for model_id in ["michiyasunaga/BioLinkBERT-base", "allenai/scibert_scivocab_uncased"]:
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModel.from_pretrained(model_id).to(device).eval()
            
            emb = generate_bert_embedding(model, tokenizer, "test", device)
            
            assert emb.shape[0] == 768, f"{model_id} expected 768D, got {emb.shape[0]}"
            logger.info(f"✅ {model_id}: {emb.shape[0]}D matches schema")
        
        logger.info("✅ All verified model dimensions match Milvus schema")
    
    def test_batch_embedding_generation(self, device):
        """Test generación de embeddings en batch"""
        from transformers import AutoModel, AutoTokenizer
        
        model_id = "michiyasunaga/BioLinkBERT-base"
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id).to(device).eval()
        
        # Batch de textos
        texts = [
            "BRCA1 tumor suppressor",
            "Kinase phosphorylation",
            "Membrane receptor signaling",
            "DNA damage repair",
        ]
        
        # Tokenizar batch
        inputs = tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=128
        ).to(device)
        
        # Inferencia
        with torch.no_grad():
            outputs = model(**inputs)
        
        # Mean pooling para batch
        attention_mask = inputs["attention_mask"]
        token_embeddings = outputs.last_hidden_state
        
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, dim=1)
        sum_mask = torch.clamp(input_mask_expanded.sum(dim=1), min=1e-9)
        mean_pooled = sum_embeddings / sum_mask
        
        # Normalizar
        embeddings = mean_pooled.cpu().numpy()
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / norms
        
        # Verificar
        assert embeddings.shape == (4, 768), f"Expected (4, 768), got {embeddings.shape}"
        
        # Verificar normalización
        for i, emb in enumerate(embeddings):
            norm = np.linalg.norm(emb)
            assert 0.99 < norm < 1.01, f"Embedding {i} not normalized: {norm}"
        
        logger.info(f"✅ Batch embedding: {embeddings.shape}, all normalized")
    
    def test_similarity_search_mock(self, device):
        """Test mock de búsqueda por similitud"""
        from transformers import AutoModel, AutoTokenizer
        
        model_id = "michiyasunaga/BioLinkBERT-base"
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id).to(device).eval()
        
        # "Base de datos" de documentos
        documents = [
            ("doc1", "BRCA1 is essential for DNA repair"),
            ("doc2", "Insulin regulates glucose metabolism"),
            ("doc3", "BRCA2 participates in homologous recombination"),
            ("doc4", "Hemoglobin carries oxygen in blood"),
            ("doc5", "TP53 is a tumor suppressor gene"),
        ]
        
        # Generar embeddings de documentos
        doc_embeddings = {}
        for doc_id, text in documents:
            emb = generate_bert_embedding(model, tokenizer, text, device)
            doc_embeddings[doc_id] = emb
        
        # Query
        query = "DNA repair genes in cancer"
        query_emb = generate_bert_embedding(model, tokenizer, query, device)
        
        # Calcular similitudes
        similarities = {}
        for doc_id, doc_emb in doc_embeddings.items():
            sim = float(np.dot(query_emb, doc_emb))
            similarities[doc_id] = sim
        
        # Ordenar por similitud
        ranked = sorted(similarities.items(), key=lambda x: x[1], reverse=True)
        
        logger.info(f"Query: '{query}'")
        for doc_id, sim in ranked:
            doc_text = next(t for d, t in documents if d == doc_id)
            logger.info(f"  {doc_id}: {sim:.4f} - '{doc_text[:40]}'")
        
        # BRCA1 y BRCA2 deberían ser más relevantes
        top_2_ids = [r[0] for r in ranked[:2]]
        assert "doc1" in top_2_ids or "doc3" in top_2_ids, \
            "Expected DNA repair documents in top results"
        
        logger.info("✅ Similarity search mock passed")


# ============================================================================
# RUN DIRECTLY
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 BSM SMOKE TESTS - HUGGINGFACE REAL INTEGRATION")
    print("=" * 60)
    
    # Detectar device
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"🔥 Using CUDA: {torch.cuda.get_device_name()}")
    else:
        device = torch.device("cpu")
        print(f"💻 Using CPU")
    
    print(f"📦 PyTorch version: {torch.__version__}")
    
    try:
        import transformers
        print(f"🤗 Transformers version: {transformers.__version__}")
    except ImportError:
        print("❌ Transformers not installed!")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("Running smoke tests...")
    print("=" * 60 + "\n")
    
    # Run pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short", "-x"]))

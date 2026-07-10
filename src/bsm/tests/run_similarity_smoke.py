#!/usr/bin/env python3
"""Quick similarity search smoke test"""
import torch
import numpy as np
from transformers import AutoModel, AutoTokenizer

print('=' * 60)
print('SMOKE TEST: Similarity Search with Real Embeddings')
print('=' * 60)

device = torch.device('cpu')

# Load BioLinkBERT (cached now)
model_id = 'michiyasunaga/BioLinkBERT-base'
print(f'\nLoading {model_id}...')
tokenizer = AutoTokenizer.from_pretrained(model_id)
model = AutoModel.from_pretrained(model_id).to(device).eval()

def get_embedding(text):
    inputs = tokenizer(text, return_tensors='pt', padding=True, truncation=True, max_length=512).to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    attention_mask = inputs['attention_mask']
    token_embeddings = outputs.last_hidden_state
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, dim=1)
    sum_mask = torch.clamp(input_mask_expanded.sum(dim=1), min=1e-9)
    embedding = (sum_embeddings / sum_mask)[0].cpu().numpy()
    return embedding / np.linalg.norm(embedding)

# Document database
print('\nCreating document embeddings...')
documents = [
    ('DOC-001', 'BRCA1 is essential for DNA repair in breast cancer cells'),
    ('DOC-002', 'Insulin binds to receptors regulating glucose metabolism'),
    ('DOC-003', 'BRCA2 participates in homologous recombination DNA repair'),
    ('DOC-004', 'Hemoglobin carries oxygen molecules in red blood cells'),
    ('DOC-005', 'TP53 tumor suppressor gene mutations in cancer'),
    ('DOC-006', 'PARP inhibitors target DNA damage repair pathways'),
]

doc_embeddings = {}
for doc_id, text in documents:
    doc_embeddings[doc_id] = get_embedding(text)
    print(f'  {doc_id}: embedded')

# Queries
queries = [
    'DNA repair genes in breast cancer',
    'oxygen transport proteins',
    'diabetes related proteins',
]

print('\n' + '-' * 60)
for query in queries:
    print(f'\nQuery: "{query}"')
    query_emb = get_embedding(query)
    
    # Calculate similarities
    results = []
    for doc_id, doc_emb in doc_embeddings.items():
        sim = float(np.dot(query_emb, doc_emb))
        results.append((doc_id, sim))
    
    # Sort by similarity
    results.sort(key=lambda x: x[1], reverse=True)
    
    print('Top 3 results:')
    for i, (doc_id, sim) in enumerate(results[:3]):
        doc_text = next(t for d, t in documents if d == doc_id)
        print(f'  {i+1}. {doc_id} (sim={sim:.4f}): {doc_text[:50]}...')

print('\n' + '=' * 60)
print('SUCCESS: Similarity search smoke test passed!')
print('=' * 60)

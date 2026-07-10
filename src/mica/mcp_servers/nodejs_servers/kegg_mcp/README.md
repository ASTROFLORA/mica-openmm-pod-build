![Logo](logo.png)

# Unofficial KEGG MCP Server

A Model Context Protocol (MCP) server that provides comprehensive access to the KEGG (Kyoto Encyclopedia of Genes and Genomes) database through its REST API.

**Developed by [Augmented Nature](https://augmentednature.ai)**

## Overview

This MCP server enables seamless integration with KEGG's extensive biological databases, providing tools for pathway analysis, gene research, compound investigation, and cross-database linking. It's designed for researchers in bioinformatics, systems biology, drug discovery, and related fields.

## Features

### Database Coverage

- **Pathways**: KEGG pathway maps and organism-specific pathways
- **Genes**: Gene information across 5000+ organisms
- **Compounds**: Chemical compounds and their properties
- **Reactions**: Biochemical reactions and enzymes
- **Diseases**: Human diseases and associated genes
- **Drugs**: Drug information and interactions
- **Cross-references**: Links between KEGG and external databases

### Tool Categories

#### Database Information & Statistics (2 tools)

- `get_database_info`: Get release information and statistics
- `list_organisms`: Get all KEGG organisms with codes and names

#### Pathway Analysis (3 tools)

- `search_pathways`: Search pathways by keywords or names
- `get_pathway_info`: Get detailed pathway information
- `get_pathway_genes`: Get genes involved in specific pathways

#### Gene Analysis (2 tools)

- `search_genes`: Search genes by name, symbol, or keywords
- `get_gene_info`: Get detailed gene information with optional sequences

#### Compound Analysis (2 tools)

- `search_compounds`: Search compounds by name, formula, or structure
- `get_compound_info`: Get detailed compound information

#### Reaction & Enzyme Analysis (4 tools)

- `search_reactions`: Search biochemical reactions by keywords
- `get_reaction_info`: Get detailed reaction information
- `search_enzymes`: Search enzymes by EC number or name
- `get_enzyme_info`: Get detailed enzyme information

#### Disease & Drug Analysis (5 tools)

- `search_diseases`: Search human diseases by name or keywords
- `get_disease_info`: Get detailed disease information
- `search_drugs`: Search drugs by name, target, or indication
- `get_drug_info`: Get detailed drug information
- `get_drug_interactions`: Find adverse drug-drug interactions

#### Module & Orthology Analysis (4 tools)

- `search_modules`: Search KEGG modules by name or function
- `get_module_info`: Get detailed module information
- `search_ko_entries`: Search KEGG Orthology entries
- `get_ko_info`: Get detailed KO information

#### Glycan Analysis (2 tools)

- `search_glycans`: Search glycan structures by name or composition
- `get_glycan_info`: Get detailed glycan information

#### BRITE Hierarchy Analysis (2 tools)

- `search_brite`: Search BRITE functional hierarchies
- `get_brite_info`: Get detailed BRITE entry information

#### Advanced Analysis Tools (4 tools)

- `get_pathway_compounds`: Get all compounds in a pathway
- `get_pathway_reactions`: Get all reactions in a pathway
- `get_compound_reactions`: Get all reactions involving a compound
- `get_gene_orthologs`: Find orthologous genes across organisms
- `batch_entry_lookup`: Process multiple KEGG entries efficiently

#### Cross-References & Integration (2 tools)

- `convert_identifiers`: Convert between KEGG and external database IDs
- `find_related_entries`: Find related entries across databases

### Resource Templates (8 templates)

- `kegg://pathway/{pathway_id}`: Pathway information
- `kegg://gene/{org}:{gene_id}`: Gene details
- `kegg://compound/{compound_id}`: Compound information
- `kegg://reaction/{reaction_id}`: Reaction details
- `kegg://disease/{disease_id}`: Disease information
- `kegg://drug/{drug_id}`: Drug information
- `kegg://organism/{org_code}`: Organism details
- `kegg://search/{database}/{query}`: Search results

## Installation

1. Clone or download this server to your local machine
2. Install dependencies:
   ```bash
   cd kegg-server
   npm install
   ```
3. Build the server:
   ```bash
   npm run build
   ```

## Usage Examples

### Pathway Analysis

```
# Search for glycolysis pathways
search_pathways(query="glycolysis")

# Get human glycolysis pathway
get_pathway_info(pathway_id="hsa00010")

# Get genes in human glycolysis pathway
get_pathway_genes(pathway_id="hsa00010")
```

### Gene Research

```
# Search for insulin genes
search_genes(query="insulin", organism_code="hsa")

# Get detailed information for human insulin gene
get_gene_info(gene_id="hsa:3630", include_sequences=true)
```

### Compound Investigation

```
# Search for glucose compounds
search_compounds(query="glucose")

# Get glucose compound information
get_compound_info(compound_id="C00031")

# Search by molecular formula
search_compounds(query="C6H12O6", search_type="formula")
```

### Cross-Database Integration

```
# Convert KEGG gene IDs to NCBI Gene IDs
convert_identifiers(source_db="hsa", target_db="ncbi-geneid")

# Find pathways related to specific genes
find_related_entries(source_db="genes", target_db="pathway", source_entries=["hsa:3630"])
```

## API Coverage

This server implements the core KEGG REST API operations:

- **INFO**: Database release information
- **LIST**: Entry lists and organism catalogs
- **FIND**: Keyword and structure-based searching
- **GET**: Detailed entry retrieval
- **CONV**: Identifier conversion between databases
- **LINK**: Cross-reference discovery

## Supported Organisms

The server supports all organisms in KEGG, including:

- **Human** (hsa): Homo sapiens
- **Mouse** (mmu): Mus musculus
- **Rat** (rno): Rattus norvegicus
- **E. coli** (eco): Escherichia coli
- **Yeast** (sce): Saccharomyces cerevisiae
- **5000+ other organisms**

## Data Types

### Pathways

- Reference pathways (map)
- Organism-specific pathways
- KEGG Orthology pathways (ko)
- Enzyme classification pathways (ec)

### Genes

- Protein-coding genes
- RNA genes
- Pseudogenes
- Gene sequences (amino acid and nucleotide)

### Compounds

- Small molecules
- Metabolites
- Chemical structures
- Molecular properties

## Error Handling

The server includes comprehensive error handling:

- Input validation for all parameters
- Graceful handling of API timeouts
- Detailed error messages for debugging
- Fallback mechanisms for optional data

## Performance Considerations

- **Rate Limiting**: Respectful API usage with appropriate delays
- **Batch Processing**: Efficient handling of multiple entries
- **Caching**: Optional caching for frequently accessed data
- **Timeouts**: 30-second timeout for API requests

## Use Cases

### Research Applications

- **Systems Biology**: Pathway analysis and network reconstruction
- **Drug Discovery**: Target identification and compound screening
- **Comparative Genomics**: Cross-species gene analysis
- **Metabolomics**: Metabolic pathway investigation
- **Disease Research**: Gene-disease association studies

### Educational Applications

- **Biochemistry Teaching**: Pathway visualization and exploration
- **Bioinformatics Training**: Database integration exercises
- **Molecular Biology**: Gene function and regulation studies

## Contributing

This server is built using the Model Context Protocol SDK. To contribute:

1. Fork the repository
2. Make your changes
3. Test thoroughly with various KEGG queries
4. Submit a pull request

## License

MIT License - see LICENSE file for details.

## Support

For issues related to:

- **Server functionality**: Check the error logs and validate input parameters
- **KEGG API**: Refer to the official KEGG REST API documentation
- **MCP Protocol**: Consult the Model Context Protocol documentation

## Version History

- **v1.0.0**: Initial release with comprehensive KEGG API coverage
  - 30 tools covering all major KEGG databases
  - 8 resource templates for dynamic data access
  - Full support for pathways, genes, compounds, reactions, enzymes, diseases, drugs, modules, glycans, and BRITE hierarchies
  - Advanced analysis tools for cross-database integration
  - Batch processing capabilities
  - Robust error handling and input validation

## Acknowledgments

- KEGG Database: Kanehisa Laboratories
- Model Context Protocol: Anthropic
- TypeScript and Node.js communities

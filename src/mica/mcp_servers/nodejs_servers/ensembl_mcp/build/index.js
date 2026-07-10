#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ErrorCode, ListResourceTemplatesRequestSchema, ListToolsRequestSchema, McpError, ReadResourceRequestSchema, } from '@modelcontextprotocol/sdk/types.js';
import axios from 'axios';
// Type guards and validation functions
const isValidGeneArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.gene_id === 'string' &&
        args.gene_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.expand === undefined || typeof args.expand === 'boolean') &&
        (args.format === undefined || ['json', 'fasta', 'gff'].includes(args.format)));
};
const isValidSequenceArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.region === 'string' &&
        args.region.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'fasta'].includes(args.format)) &&
        (args.mask === undefined || ['hard', 'soft'].includes(args.mask)) &&
        (args.multiple_sequences === undefined || typeof args.multiple_sequences === 'boolean'));
};
const isValidSearchArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.query === 'string' &&
        args.query.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.feature === undefined || ['gene', 'transcript'].includes(args.feature)) &&
        (args.biotype === undefined || typeof args.biotype === 'string') &&
        (args.limit === undefined || (typeof args.limit === 'number' && args.limit > 0 && args.limit <= 200)));
};
const isValidVariantArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.region === 'string' &&
        args.region.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'vcf'].includes(args.format)) &&
        (args.consequence_type === undefined ||
            (Array.isArray(args.consequence_type) && args.consequence_type.every((c) => typeof c === 'string'))));
};
const isValidHomologArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.gene_id === 'string' &&
        args.gene_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.target_species === undefined || typeof args.target_species === 'string') &&
        (args.type === undefined || ['orthologues', 'paralogues', 'all'].includes(args.type)) &&
        (args.format === undefined || ['json', 'xml'].includes(args.format)));
};
const isValidRegulatoryArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.region === 'string' &&
        args.region.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.feature_type === undefined || typeof args.feature_type === 'string') &&
        (args.cell_type === undefined || typeof args.cell_type === 'string'));
};
const isValidXrefArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.gene_id === 'string' &&
        args.gene_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.external_db === undefined || typeof args.external_db === 'string') &&
        (args.all_levels === undefined || typeof args.all_levels === 'boolean'));
};
const isValidBatchArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        Array.isArray(args.gene_ids) &&
        args.gene_ids.length > 0 &&
        args.gene_ids.length <= 200 &&
        args.gene_ids.every((id) => typeof id === 'string' && id.length > 0) &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'xml'].includes(args.format)));
};
const isValidAssemblyArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.bands === undefined || typeof args.bands === 'boolean'));
};
const isValidTranscriptArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.gene_id === 'string' &&
        args.gene_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.canonical_only === undefined || typeof args.canonical_only === 'boolean'));
};
const isValidCdsArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.transcript_id === 'string' &&
        args.transcript_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'fasta'].includes(args.format)));
};
const isValidGeneTreeArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.gene_id === 'string' &&
        args.gene_id.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'newick', 'phyloxml'].includes(args.format)) &&
        (args.clusterset_id === undefined || typeof args.clusterset_id === 'string'));
};
const isValidMotifArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.region === 'string' &&
        args.region.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.binding_matrix === undefined || typeof args.binding_matrix === 'string'));
};
const isValidTranslateArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.sequence === 'string' &&
        args.sequence.length > 0 &&
        (args.genetic_code === undefined || (typeof args.genetic_code === 'number' && args.genetic_code >= 1 && args.genetic_code <= 31)));
};
const isValidVariantConsequenceArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        Array.isArray(args.variants) &&
        args.variants.length > 0 &&
        args.variants.every((v) => typeof v === 'string' && v.length > 0) &&
        (args.species === undefined || typeof args.species === 'string'));
};
const isValidMapCoordinatesArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        typeof args.region === 'string' &&
        args.region.length > 0 &&
        (args.species === undefined || typeof args.species === 'string') &&
        typeof args.target_assembly === 'string' &&
        args.target_assembly.length > 0);
};
const isValidBatchSequenceArgs = (args) => {
    return (typeof args === 'object' &&
        args !== null &&
        Array.isArray(args.regions) &&
        args.regions.length > 0 &&
        args.regions.length <= 50 &&
        args.regions.every((r) => typeof r === 'string' && r.length > 0) &&
        (args.species === undefined || typeof args.species === 'string') &&
        (args.format === undefined || ['json', 'fasta'].includes(args.format)));
};
class EnsemblServer {
    constructor() {
        this.server = new Server({
            name: 'ensembl-server',
            version: '1.0.0',
        }, {
            capabilities: {
                resources: {},
                tools: {},
            },
        });
        // Initialize Ensembl REST API client
        this.apiClient = axios.create({
            baseURL: 'https://rest.ensembl.org',
            timeout: 30000,
            headers: {
                'User-Agent': 'Ensembl-MCP-Server/1.0.0',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
        });
        this.setupResourceHandlers();
        this.setupToolHandlers();
        // Error handling
        this.server.onerror = (error) => console.error('[MCP Error]', error);
        process.on('SIGINT', async () => {
            await this.server.close();
            process.exit(0);
        });
    }
    setupResourceHandlers() {
        // List available resource templates
        this.server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
            resourceTemplates: [
                {
                    uriTemplate: 'ensembl://gene/{gene_id}',
                    name: 'Ensembl gene information',
                    mimeType: 'application/json',
                    description: 'Complete gene information and annotations from Ensembl',
                },
                {
                    uriTemplate: 'ensembl://transcript/{transcript_id}',
                    name: 'Ensembl transcript details',
                    mimeType: 'application/json',
                    description: 'Transcript structure and exon information',
                },
                {
                    uriTemplate: 'ensembl://sequence/{region}',
                    name: 'Genomic sequence',
                    mimeType: 'text/plain',
                    description: 'DNA sequence for genomic coordinates',
                },
                {
                    uriTemplate: 'ensembl://variants/{region}',
                    name: 'Genetic variants',
                    mimeType: 'application/json',
                    description: 'Variants and their consequences in genomic region',
                },
                {
                    uriTemplate: 'ensembl://homologues/{gene_id}',
                    name: 'Gene homologs',
                    mimeType: 'application/json',
                    description: 'Orthologous and paralogous genes across species',
                },
                {
                    uriTemplate: 'ensembl://regulatory/{region}',
                    name: 'Regulatory features',
                    mimeType: 'application/json',
                    description: 'Regulatory elements in genomic region',
                },
                {
                    uriTemplate: 'ensembl://assembly/{species}',
                    name: 'Assembly information',
                    mimeType: 'application/json',
                    description: 'Genome assembly and species metadata',
                },
            ],
        }));
        // Handle resource requests
        this.server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
            const uri = request.params.uri;
            // Handle gene info requests
            const geneMatch = uri.match(/^ensembl:\/\/gene\/([^\/]+)$/);
            if (geneMatch) {
                const geneId = geneMatch[1];
                try {
                    const result = await this.handleLookupGene({ gene_id: geneId });
                    return {
                        contents: [
                            {
                                uri: request.params.uri,
                                mimeType: 'application/json',
                                text: result.content[0].text,
                            },
                        ],
                    };
                }
                catch (error) {
                    throw new McpError(ErrorCode.InternalError, `Failed to fetch gene ${geneId}: ${error instanceof Error ? error.message : 'Unknown error'}`);
                }
            }
            // Handle sequence requests
            const sequenceMatch = uri.match(/^ensembl:\/\/sequence\/(.+)$/);
            if (sequenceMatch) {
                const region = decodeURIComponent(sequenceMatch[1]);
                try {
                    const result = await this.handleGetSequence({ region });
                    return {
                        contents: [
                            {
                                uri: request.params.uri,
                                mimeType: 'text/plain',
                                text: result.content[0].text,
                            },
                        ],
                    };
                }
                catch (error) {
                    throw new McpError(ErrorCode.InternalError, `Failed to fetch sequence for ${region}: ${error instanceof Error ? error.message : 'Unknown error'}`);
                }
            }
            // Handle homolog requests
            const homologMatch = uri.match(/^ensembl:\/\/homologues\/([^\/]+)$/);
            if (homologMatch) {
                const geneId = homologMatch[1];
                try {
                    const result = await this.handleGetHomologs({ gene_id: geneId });
                    return {
                        contents: [
                            {
                                uri: request.params.uri,
                                mimeType: 'application/json',
                                text: result.content[0].text,
                            },
                        ],
                    };
                }
                catch (error) {
                    throw new McpError(ErrorCode.InternalError, `Failed to fetch homologs for ${geneId}: ${error instanceof Error ? error.message : 'Unknown error'}`);
                }
            }
            throw new McpError(ErrorCode.InvalidRequest, `Invalid URI format: ${uri}`);
        });
    }
    setupToolHandlers() {
        this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
            tools: [
                // Gene & Transcript Information
                {
                    name: 'lookup_gene',
                    description: 'Get detailed gene information by stable ID or symbol',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_id: { type: 'string', description: 'Ensembl gene ID or gene symbol (e.g., ENSG00000139618, BRCA2)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            expand: { type: 'boolean', description: 'Include transcript and exon details (default: false)' },
                            format: { type: 'string', enum: ['json', 'fasta', 'gff'], description: 'Output format (default: json)' },
                        },
                        required: ['gene_id'],
                    },
                },
                {
                    name: 'get_transcripts',
                    description: 'Get all transcripts for a gene with detailed structure',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_id: { type: 'string', description: 'Ensembl gene ID' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            canonical_only: { type: 'boolean', description: 'Return only canonical transcript (default: false)' },
                        },
                        required: ['gene_id'],
                    },
                },
                {
                    name: 'search_genes',
                    description: 'Search for genes by name, description, or identifier',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            query: { type: 'string', description: 'Search term (gene name, description, or partial match)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            feature: { type: 'string', enum: ['gene', 'transcript'], description: 'Feature type to search (default: gene)' },
                            biotype: { type: 'string', description: 'Filter by biotype (e.g., protein_coding, lncRNA)' },
                            limit: { type: 'number', description: 'Maximum results (1-200, default: 25)', minimum: 1, maximum: 200 },
                        },
                        required: ['query'],
                    },
                },
                // Sequence Data
                {
                    name: 'get_sequence',
                    description: 'Get DNA sequence for genomic coordinates or gene/transcript ID',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            region: { type: 'string', description: 'Genomic region (chr:start-end) or feature ID' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'fasta'], description: 'Output format (default: fasta)' },
                            mask: { type: 'string', enum: ['hard', 'soft'], description: 'Repeat masking type (optional)' },
                            multiple_sequences: { type: 'boolean', description: 'Return multiple sequences if applicable (default: false)' },
                        },
                        required: ['region'],
                    },
                },
                {
                    name: 'get_cds_sequence',
                    description: 'Get coding sequence (CDS) for a transcript',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            transcript_id: { type: 'string', description: 'Ensembl transcript ID' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'fasta'], description: 'Output format (default: fasta)' },
                        },
                        required: ['transcript_id'],
                    },
                },
                {
                    name: 'translate_sequence',
                    description: 'Translate DNA sequence to protein sequence',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            sequence: { type: 'string', description: 'DNA sequence to translate' },
                            genetic_code: { type: 'number', description: 'Genetic code table (default: 1 for standard)', minimum: 1, maximum: 31 },
                        },
                        required: ['sequence'],
                    },
                },
                // Comparative Genomics
                {
                    name: 'get_homologs',
                    description: 'Find orthologous and paralogous genes across species',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_id: { type: 'string', description: 'Ensembl gene ID' },
                            species: { type: 'string', description: 'Source species name (default: homo_sapiens)' },
                            target_species: { type: 'string', description: 'Target species to search (optional)' },
                            type: { type: 'string', enum: ['orthologues', 'paralogues', 'all'], description: 'Homolog type (default: all)' },
                            format: { type: 'string', enum: ['json', 'xml'], description: 'Output format (default: json)' },
                        },
                        required: ['gene_id'],
                    },
                },
                {
                    name: 'get_gene_tree',
                    description: 'Get phylogenetic tree for gene family',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_id: { type: 'string', description: 'Ensembl gene ID' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'newick', 'phyloxml'], description: 'Tree format (default: json)' },
                            clusterset_id: { type: 'string', description: 'Specific clusterset ID (optional)' },
                        },
                        required: ['gene_id'],
                    },
                },
                // Variant Data
                {
                    name: 'get_variants',
                    description: 'Get genetic variants in a genomic region',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            region: { type: 'string', description: 'Genomic region (chr:start-end)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'vcf'], description: 'Output format (default: json)' },
                            consequence_type: { type: 'array', items: { type: 'string' }, description: 'Filter by consequence types' },
                        },
                        required: ['region'],
                    },
                },
                {
                    name: 'get_variant_consequences',
                    description: 'Predict consequences of variants on genes and transcripts',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            variants: { type: 'array', items: { type: 'string' }, description: 'Variant IDs or HGVS notation' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                        },
                        required: ['variants'],
                    },
                },
                // Regulatory Features
                {
                    name: 'get_regulatory_features',
                    description: 'Get regulatory elements (enhancers, promoters, TFBS) in genomic region',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            region: { type: 'string', description: 'Genomic region (chr:start-end)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            feature_type: { type: 'string', description: 'Regulatory feature type (optional)' },
                            cell_type: { type: 'string', description: 'Cell type context (optional)' },
                        },
                        required: ['region'],
                    },
                },
                {
                    name: 'get_motif_features',
                    description: 'Get transcription factor binding motifs in genomic region',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            region: { type: 'string', description: 'Genomic region (chr:start-end)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            binding_matrix: { type: 'string', description: 'Specific binding matrix (optional)' },
                        },
                        required: ['region'],
                    },
                },
                // Cross-References & Annotations
                {
                    name: 'get_xrefs',
                    description: 'Get external database cross-references for genes',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_id: { type: 'string', description: 'Ensembl gene ID' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            external_db: { type: 'string', description: 'Specific external database (optional)' },
                            all_levels: { type: 'boolean', description: 'Include transcript and translation xrefs (default: false)' },
                        },
                        required: ['gene_id'],
                    },
                },
                {
                    name: 'map_coordinates',
                    description: 'Convert coordinates between genome assemblies',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            region: { type: 'string', description: 'Genomic region (chr:start-end)' },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            target_assembly: { type: 'string', description: 'Target assembly name' },
                        },
                        required: ['region', 'target_assembly'],
                    },
                },
                // Species & Assembly Information
                {
                    name: 'list_species',
                    description: 'Get list of available species and assemblies',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            division: { type: 'string', description: 'Ensembl division (e.g., vertebrates, plants, fungi)' },
                        },
                        required: [],
                    },
                },
                {
                    name: 'get_assembly_info',
                    description: 'Get genome assembly information and statistics',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            bands: { type: 'boolean', description: 'Include chromosome banding patterns (default: false)' },
                        },
                        required: [],
                    },
                },
                {
                    name: 'get_karyotype',
                    description: 'Get chromosome information and karyotype',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                        },
                        required: [],
                    },
                },
                // Batch Processing
                {
                    name: 'batch_gene_lookup',
                    description: 'Look up multiple genes simultaneously',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            gene_ids: { type: 'array', items: { type: 'string' }, description: 'List of gene IDs (max 200)', minItems: 1, maxItems: 200 },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'xml'], description: 'Output format (default: json)' },
                        },
                        required: ['gene_ids'],
                    },
                },
                {
                    name: 'batch_sequence_fetch',
                    description: 'Fetch sequences for multiple regions or features',
                    inputSchema: {
                        type: 'object',
                        properties: {
                            regions: { type: 'array', items: { type: 'string' }, description: 'List of regions or feature IDs (max 50)', minItems: 1, maxItems: 50 },
                            species: { type: 'string', description: 'Species name (default: homo_sapiens)' },
                            format: { type: 'string', enum: ['json', 'fasta'], description: 'Output format (default: fasta)' },
                        },
                        required: ['regions'],
                    },
                },
            ],
        }));
        this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
            const { name, arguments: args } = request.params;
            switch (name) {
                // Gene & Transcript Information
                case 'lookup_gene':
                    return this.handleLookupGene(args);
                case 'get_transcripts':
                    return this.handleGetTranscripts(args);
                case 'search_genes':
                    return this.handleSearchGenes(args);
                // Sequence Data
                case 'get_sequence':
                    return this.handleGetSequence(args);
                case 'get_cds_sequence':
                    return this.handleGetCdsSequence(args);
                case 'translate_sequence':
                    return this.handleTranslateSequence(args);
                // Comparative Genomics
                case 'get_homologs':
                    return this.handleGetHomologs(args);
                case 'get_gene_tree':
                    return this.handleGetGeneTree(args);
                // Variant Data
                case 'get_variants':
                    return this.handleGetVariants(args);
                case 'get_variant_consequences':
                    return this.handleGetVariantConsequences(args);
                // Regulatory Features
                case 'get_regulatory_features':
                    return this.handleGetRegulatoryFeatures(args);
                case 'get_motif_features':
                    return this.handleGetMotifFeatures(args);
                // Cross-References & Annotations
                case 'get_xrefs':
                    return this.handleGetXrefs(args);
                case 'map_coordinates':
                    return this.handleMapCoordinates(args);
                // Species & Assembly Information
                case 'list_species':
                    return this.handleListSpecies(args);
                case 'get_assembly_info':
                    return this.handleGetAssemblyInfo(args);
                case 'get_karyotype':
                    return this.handleGetKaryotype(args);
                // Batch Processing
                case 'batch_gene_lookup':
                    return this.handleBatchGeneLookup(args);
                case 'batch_sequence_fetch':
                    return this.handleBatchSequenceFetch(args);
                default:
                    throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
            }
        });
    }
    // Utility methods
    getDefaultSpecies(species) {
        return species || 'homo_sapiens';
    }
    formatGenomicRegion(region) {
        // Handle different region formats and ensure proper formatting
        // Support formats like: chr1:1000-2000, 1:1000-2000, ENSG00000139618
        if (region.includes(':') && region.includes('-')) {
            // Already in proper format
            return region;
        }
        else if (region.startsWith('ENS')) {
            // Gene/transcript/exon ID
            return region;
        }
        else {
            // Assume it's a chromosome name, return as-is
            return region;
        }
    }
    handleError(error, context) {
        const message = error.response?.data?.error || error.message || 'Unknown error';
        return {
            content: [
                {
                    type: 'text',
                    text: `Error ${context}: ${message}`,
                },
            ],
            isError: true,
        };
    }
    // Tool handler implementations
    async handleLookupGene(args) {
        if (!isValidGeneArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid gene lookup arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'json';
            let endpoint = `/lookup/id/${args.gene_id}`;
            const params = { species };
            if (args.expand) {
                params.expand = 1;
            }
            if (format !== 'json') {
                params.format = format;
            }
            const response = await this.apiClient.get(endpoint, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: typeof response.data === 'object'
                            ? JSON.stringify(response.data, null, 2)
                            : String(response.data),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'looking up gene');
        }
    }
    async handleGetTranscripts(args) {
        if (!isValidTranscriptArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid transcript arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const response = await this.apiClient.get(`/lookup/id/${args.gene_id}`, {
                params: { species, expand: 1 },
            });
            const gene = response.data;
            let transcripts = gene.Transcript || [];
            if (args.canonical_only) {
                transcripts = transcripts.filter((t) => t.is_canonical === 1);
            }
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            gene_id: gene.id,
                            gene_name: gene.display_name,
                            transcript_count: transcripts.length,
                            transcripts: transcripts,
                        }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching transcripts');
        }
    }
    async handleSearchGenes(args) {
        if (!isValidSearchArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const feature = args.feature || 'gene';
            const limit = args.limit || 25;
            const params = {
                q: args.query,
                species,
                feature,
                limit,
            };
            if (args.biotype) {
                params.biotype = args.biotype;
            }
            const response = await this.apiClient.get('/search', { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'searching genes');
        }
    }
    async handleGetSequence(args) {
        if (!isValidSequenceArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid sequence arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'fasta';
            const region = this.formatGenomicRegion(args.region);
            let endpoint;
            const params = {};
            if (region.startsWith('ENS')) {
                // Feature ID
                endpoint = `/sequence/id/${region}`;
                params.type = 'genomic';
            }
            else {
                // Genomic region
                endpoint = `/sequence/region/${species}/${region}`;
            }
            if (args.mask) {
                params.mask = args.mask;
            }
            if (args.multiple_sequences) {
                params.multiple_sequences = 1;
            }
            const response = await this.apiClient.get(endpoint, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: format === 'json'
                            ? JSON.stringify(response.data, null, 2)
                            : typeof response.data === 'string'
                                ? response.data
                                : JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching sequence');
        }
    }
    async handleGetCdsSequence(args) {
        if (!isValidCdsArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid CDS sequence arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'fasta';
            const response = await this.apiClient.get(`/sequence/id/${args.transcript_id}`, {
                params: {
                    type: 'cds',
                    species,
                },
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: format === 'json'
                            ? JSON.stringify(response.data, null, 2)
                            : typeof response.data === 'string'
                                ? response.data
                                : JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching CDS sequence');
        }
    }
    async handleTranslateSequence(args) {
        try {
            const geneticCode = args.genetic_code || 1;
            // Simple translation implementation
            const codonTable = {
                'TTT': 'F', 'TTC': 'F', 'TTA': 'L', 'TTG': 'L',
                'TCT': 'S', 'TCC': 'S', 'TCA': 'S', 'TCG': 'S',
                'TAT': 'Y', 'TAC': 'Y', 'TAA': '*', 'TAG': '*',
                'TGT': 'C', 'TGC': 'C', 'TGA': '*', 'TGG': 'W',
                'CTT': 'L', 'CTC': 'L', 'CTA': 'L', 'CTG': 'L',
                'CCT': 'P', 'CCC': 'P', 'CCA': 'P', 'CCG': 'P',
                'CAT': 'H', 'CAC': 'H', 'CAA': 'Q', 'CAG': 'Q',
                'CGT': 'R', 'CGC': 'R', 'CGA': 'R', 'CGG': 'R',
                'ATT': 'I', 'ATC': 'I', 'ATA': 'I', 'ATG': 'M',
                'ACT': 'T', 'ACC': 'T', 'ACA': 'T', 'ACG': 'T',
                'AAT': 'N', 'AAC': 'N', 'AAA': 'K', 'AAG': 'K',
                'AGT': 'S', 'AGC': 'S', 'AGA': 'R', 'AGG': 'R',
                'GTT': 'V', 'GTC': 'V', 'GTA': 'V', 'GTG': 'V',
                'GCT': 'A', 'GCC': 'A', 'GCA': 'A', 'GCG': 'A',
                'GAT': 'D', 'GAC': 'D', 'GAA': 'E', 'GAG': 'E',
                'GGT': 'G', 'GGC': 'G', 'GGA': 'G', 'GGG': 'G',
            };
            const sequence = args.sequence.toUpperCase().replace(/[^ATCG]/g, '');
            let protein = '';
            for (let i = 0; i < sequence.length - 2; i += 3) {
                const codon = sequence.substr(i, 3);
                if (codon.length === 3) {
                    protein += codonTable[codon] || 'X';
                }
            }
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            input_sequence: args.sequence,
                            cleaned_sequence: sequence,
                            protein_sequence: protein,
                            genetic_code: geneticCode,
                            length: protein.length,
                        }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'translating sequence');
        }
    }
    async handleGetHomologs(args) {
        if (!isValidHomologArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid homolog arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            // Use overlap endpoint to get gene information from different species
            // This provides comparative information by looking up the same gene in different organisms
            const geneResponse = await this.apiClient.get(`/lookup/id/${args.gene_id}`, {
                params: { species }
            });
            const gene = geneResponse.data;
            // Get orthologs by looking up the same gene symbol in other species
            const targetSpecies = args.target_species || 'mus_musculus'; // Default to mouse
            try {
                const orthologResponse = await this.apiClient.get(`/lookup/symbol/${targetSpecies}/${gene.display_name}`);
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify({
                                source_gene: {
                                    id: gene.id,
                                    symbol: gene.display_name,
                                    species: species,
                                    description: gene.description,
                                    location: `${gene.seq_region_name}:${gene.start}-${gene.end}`,
                                    biotype: gene.biotype
                                },
                                ortholog: {
                                    id: orthologResponse.data.id,
                                    symbol: orthologResponse.data.display_name,
                                    species: targetSpecies,
                                    description: orthologResponse.data.description,
                                    location: `${orthologResponse.data.seq_region_name}:${orthologResponse.data.start}-${orthologResponse.data.end}`,
                                    biotype: orthologResponse.data.biotype
                                },
                                analysis: {
                                    method: 'Gene symbol ortholog lookup',
                                    conservation: 'Symbol-based orthology',
                                    note: 'Genes with same symbol across species are typically orthologs'
                                }
                            }, null, 2),
                        },
                    ],
                };
            }
            catch (orthologError) {
                // Return information about the source gene even if ortholog not found
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify({
                                source_gene: {
                                    id: gene.id,
                                    symbol: gene.display_name,
                                    species: species,
                                    description: gene.description,
                                    location: `${gene.seq_region_name}:${gene.start}-${gene.end}`,
                                    biotype: gene.biotype
                                },
                                ortholog_search: {
                                    target_species: targetSpecies,
                                    result: 'No ortholog found with same gene symbol',
                                    suggestion: 'Try different target species or use gene family analysis'
                                },
                                available_data: {
                                    gene_info: 'Complete source gene information available',
                                    cross_references: 'Use get_xrefs tool for external database links',
                                    sequences: 'Use get_sequence tool for sequence comparison'
                                }
                            }, null, 2),
                        },
                    ],
                };
            }
        }
        catch (error) {
            return this.handleError(error, 'fetching comparative gene data');
        }
    }
    async handleGetGeneTree(args) {
        if (!isValidGeneTreeArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid gene tree arguments');
        }
        try {
            const format = args.format || 'json';
            let endpoint = `/genetree/id/${args.gene_id}`;
            const params = {};
            if (args.clusterset_id) {
                params.clusterset_id = args.clusterset_id;
            }
            if (format !== 'json') {
                params.format = format;
            }
            const response = await this.apiClient.get(endpoint, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: typeof response.data === 'object'
                            ? JSON.stringify(response.data, null, 2)
                            : String(response.data),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching gene tree');
        }
    }
    async handleGetVariants(args) {
        if (!isValidVariantArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid variant arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'json';
            const region = this.formatGenomicRegion(args.region);
            // Try overlap endpoint first as variation/region may not have data for all regions
            try {
                const response = await this.apiClient.get(`/overlap/region/${species}/${region}`, {
                    params: { feature: 'variation' }
                });
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
            catch (overlapError) {
                // Fallback to variation endpoint
                const params = { format };
                if (args.consequence_type) {
                    params.consequence_type = args.consequence_type.join(',');
                }
                const response = await this.apiClient.get(`/variation/region/${species}/${region}`, { params });
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
        }
        catch (error) {
            return this.handleError(error, 'fetching variants');
        }
    }
    async handleGetVariantConsequences(args) {
        try {
            const species = this.getDefaultSpecies(args.species);
            const variants = args.variants.join('\n');
            const response = await this.apiClient.post(`/vep/species/${species}/region`, variants, {
                headers: {
                    'Content-Type': 'text/plain',
                },
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'predicting variant consequences');
        }
    }
    async handleGetRegulatoryFeatures(args) {
        if (!isValidRegulatoryArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid regulatory feature arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const region = this.formatGenomicRegion(args.region);
            // Try overlap endpoint for regulatory features
            try {
                const response = await this.apiClient.get(`/overlap/region/${species}/${region}`, {
                    params: { feature: 'regulatory' }
                });
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
            catch (overlapError) {
                // Alternative: try the overlap endpoint with different feature types
                const features = ['gene', 'transcript'];
                const results = [];
                for (const feature of features) {
                    try {
                        const response = await this.apiClient.get(`/overlap/region/${species}/${region}`, {
                            params: { feature }
                        });
                        results.push({ feature, data: response.data });
                    }
                    catch (e) {
                        // Continue to next feature
                    }
                }
                return {
                    content: [
                        {
                            type: 'text',
                            text: JSON.stringify({
                                message: 'Regulatory features not available, showing overlapping genomic features',
                                features: results
                            }, null, 2),
                        },
                    ],
                };
            }
        }
        catch (error) {
            return this.handleError(error, 'fetching regulatory features');
        }
    }
    async handleGetMotifFeatures(args) {
        if (!isValidMotifArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid motif feature arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const region = this.formatGenomicRegion(args.region);
            const params = {};
            if (args.binding_matrix) {
                params.binding_matrix = args.binding_matrix;
            }
            const response = await this.apiClient.get(`/regulatory/species/${species}/microarray/${region}`, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching motif features');
        }
    }
    async handleGetXrefs(args) {
        if (!isValidXrefArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid xref arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const params = { species };
            if (args.external_db) {
                params.external_db = args.external_db;
            }
            if (args.all_levels) {
                params.all_levels = 1;
            }
            const response = await this.apiClient.get(`/xrefs/id/${args.gene_id}`, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching cross-references');
        }
    }
    async handleMapCoordinates(args) {
        try {
            const species = this.getDefaultSpecies(args.species);
            const region = this.formatGenomicRegion(args.region);
            const response = await this.apiClient.get(`/map/coords/${species}/${args.target_assembly}/${region}`);
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'mapping coordinates');
        }
    }
    async handleListSpecies(args) {
        try {
            const params = {};
            if (args.division) {
                params.division = args.division;
            }
            const response = await this.apiClient.get('/info/species', { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'listing species');
        }
    }
    async handleGetAssemblyInfo(args) {
        if (!isValidAssemblyArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid assembly info arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const params = {};
            if (args.bands) {
                params.bands = 1;
            }
            const response = await this.apiClient.get(`/info/assembly/${species}`, { params });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching assembly info');
        }
    }
    async handleGetKaryotype(args) {
        if (!isValidAssemblyArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid karyotype arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const response = await this.apiClient.get(`/info/assembly/${species}`, {
                params: { bands: 1 },
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            species,
                            assembly_name: response.data.assembly_name,
                            karyotype: response.data.karyotype || [],
                            chromosomes: response.data.top_level_region || [],
                        }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'fetching karyotype');
        }
    }
    async handleBatchGeneLookup(args) {
        if (!isValidBatchArgs(args)) {
            throw new McpError(ErrorCode.InvalidParams, 'Invalid batch gene lookup arguments');
        }
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'json';
            const geneData = { ids: args.gene_ids };
            const response = await this.apiClient.post('/lookup/id', geneData, {
                params: { species, format },
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response.data, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'batch gene lookup');
        }
    }
    async handleBatchSequenceFetch(args) {
        try {
            const species = this.getDefaultSpecies(args.species);
            const format = args.format || 'fasta';
            const results = [];
            for (const region of args.regions) {
                try {
                    const sequenceResult = await this.handleGetSequence({
                        region,
                        species,
                        format,
                    });
                    results.push({
                        region,
                        success: true,
                        data: JSON.parse(sequenceResult.content[0].text),
                    });
                }
                catch (error) {
                    results.push({
                        region,
                        success: false,
                        error: error instanceof Error ? error.message : 'Unknown error',
                    });
                }
            }
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({ batch_results: results }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return this.handleError(error, 'batch sequence fetch');
        }
    }
    async run() {
        const transport = new StdioServerTransport();
        await this.server.connect(transport);
        console.error('Ensembl MCP server running on stdio');
    }
}
const server = new EnsemblServer();
server.run().catch(console.error);
//# sourceMappingURL=index.js.map
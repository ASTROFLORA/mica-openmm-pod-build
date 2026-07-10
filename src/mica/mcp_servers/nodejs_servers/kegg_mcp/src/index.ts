#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ErrorCode,
  ListResourcesRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListToolsRequestSchema,
  McpError,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import axios, { AxiosInstance } from 'axios';

// KEGG API interfaces
interface KEGGListResult {
  [key: string]: string;
}

interface KEGGSearchResult {
  [key: string]: string;
}

interface KEGGPathwayInfo {
  entry: string;
  name: string;
  description?: string;
  class?: string;
  pathway_map?: string;
  module?: string[];
  disease?: string[];
  drug?: string[];
  dblinks?: { [key: string]: string[] };
  organism?: string;
  gene?: { [key: string]: string };
  compound?: { [key: string]: string };
  reference?: Array<{
    pmid?: string;
    authors?: string;
    title?: string;
    journal?: string;
  }>;
}

interface KEGGGeneInfo {
  entry: string;
  name: string;
  definition?: string;
  orthology?: { [key: string]: string };
  organism?: string;
  pathway?: { [key: string]: string };
  brite?: { [key: string]: string };
  position?: string;
  motif?: { [key: string]: string[] };
  dblinks?: { [key: string]: string[] };
  aaseq?: string;
  ntseq?: string;
}

interface KEGGCompoundInfo {
  entry: string;
  name: string[];
  formula?: string;
  exact_mass?: number;
  mol_weight?: number;
  remark?: string;
  reaction?: { [key: string]: string };
  pathway?: { [key: string]: string };
  enzyme?: { [key: string]: string };
  brite?: { [key: string]: string };
  dblinks?: { [key: string]: string[] };
}

// Type guards and validation functions
const isValidSearchArgs = (
  args: any
): args is { query: string; organism_code?: string; max_results?: number; search_type?: string } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    typeof args.query === 'string' &&
    args.query.length > 0 &&
    (args.organism_code === undefined || typeof args.organism_code === 'string') &&
    (args.max_results === undefined || (typeof args.max_results === 'number' && args.max_results > 0 && args.max_results <= 1000)) &&
    (args.search_type === undefined || typeof args.search_type === 'string')
  );
};

const isValidPathwayInfoArgs = (
  args: any
): args is { pathway_id: string; format?: string } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    typeof args.pathway_id === 'string' &&
    args.pathway_id.length > 0 &&
    (args.format === undefined || ['json', 'kgml', 'image', 'conf', 'aaseq', 'ntseq'].includes(args.format))
  );
};

const isValidOrganismArgs = (
  args: any
): args is { organism_code?: string; limit?: number } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    (args.organism_code === undefined || typeof args.organism_code === 'string') &&
    (args.limit === undefined || (typeof args.limit === 'number' && args.limit > 0 && args.limit <= 1000))
  );
};

const isValidPathwayArgs = (
  args: any
): args is { pathway_id?: string; organism_code?: string; include_genes?: boolean; include_compounds?: boolean } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    (args.pathway_id === undefined || typeof args.pathway_id === 'string') &&
    (args.organism_code === undefined || typeof args.organism_code === 'string') &&
    (args.include_genes === undefined || typeof args.include_genes === 'boolean') &&
    (args.include_compounds === undefined || typeof args.include_compounds === 'boolean')
  );
};

const isValidGeneArgs = (
  args: any
): args is { gene_id?: string; organism_code?: string; include_sequences?: boolean } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    (args.gene_id === undefined || typeof args.gene_id === 'string') &&
    (args.organism_code === undefined || typeof args.organism_code === 'string') &&
    (args.include_sequences === undefined || typeof args.include_sequences === 'boolean')
  );
};

const isValidBatchArgs = (
  args: any
): args is { entry_ids: string[]; operation?: string } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    Array.isArray(args.entry_ids) &&
    args.entry_ids.length > 0 &&
    args.entry_ids.length <= 100 &&
    args.entry_ids.every((id: any) => typeof id === 'string' && id.length > 0) &&
    (args.operation === undefined || ['info', 'sequence', 'pathway', 'link'].includes(args.operation))
  );
};

const isValidBriteSearchArgs = (
  args: any
): args is { query: string; hierarchy_type?: string; max_results?: number } => {
  return (
    typeof args === 'object' &&
    args !== null &&
    typeof args.query === 'string' &&
    args.query.length > 0 &&
    (args.hierarchy_type === undefined || ['br', 'ko', 'jp'].includes(args.hierarchy_type)) &&
    (args.max_results === undefined || (typeof args.max_results === 'number' && args.max_results > 0 && args.max_results <= 1000))
  );
};

class KEGGServer {
  private server: Server;
  private apiClient: AxiosInstance;

  constructor() {
    this.server = new Server(
      {
        name: 'kegg-server',
        version: '1.0.0',
      },
      {
        capabilities: {
          resources: {},
          tools: {},
        },
      }
    );

    // Initialize KEGG REST API client
    this.apiClient = axios.create({
      baseURL: 'https://rest.kegg.jp',
      timeout: 30000,
      headers: {
        'User-Agent': 'KEGG-MCP-Server/1.0.0',
        'Accept': 'text/plain',
      },
    });

    this.setupResourceHandlers();
    this.setupToolHandlers();

    // Error handling
    this.server.onerror = (error: any) => console.error('[MCP Error]', error);
    process.on('SIGINT', async () => {
      await this.server.close();
      process.exit(0);
    });
  }

  // Utility methods for parsing KEGG data
  private parseKEGGEntry(data: string): any {
    const lines = data.split('\n');
    const result: any = {};

    for (const line of lines) {
      if (line.startsWith('ENTRY')) {
        const parts = line.split(/\s+/);
        result.entry = parts[1];
        result.type = parts[2];
      } else if (line.startsWith('NAME')) {
        result.name = line.substring(12).trim();
      } else if (line.startsWith('DEFINITION')) {
        result.definition = line.substring(12).trim();
      } else if (line.startsWith('FORMULA')) {
        result.formula = line.substring(12).trim();
      } else if (line.startsWith('PATHWAY')) {
        if (!result.pathway) result.pathway = {};
        const match = line.match(/PATHWAY\s+(\S+)\s+(.+)/);
        if (match) {
          result.pathway[match[1]] = match[2];
        }
      } else if (line.startsWith('GENE')) {
        if (!result.gene) result.gene = {};
        const match = line.match(/GENE\s+(\S+)\s+(.+)/);
        if (match) {
          result.gene[match[1]] = match[2];
        }
      } else if (line.startsWith('COMPOUND')) {
        if (!result.compound) result.compound = {};
        const match = line.match(/COMPOUND\s+(\S+)\s+(.+)/);
        if (match) {
          result.compound[match[1]] = match[2];
        }
      } else if (line.startsWith('REACTION')) {
        if (!result.reaction) result.reaction = {};
        const match = line.match(/REACTION\s+(\S+)\s+(.+)/);
        if (match) {
          result.reaction[match[1]] = match[2];
        }
      } else if (line.startsWith('ORTHOLOGY')) {
        if (!result.orthology) result.orthology = {};
        const match = line.match(/ORTHOLOGY\s+(\S+)\s+(.+)/);
        if (match) {
          result.orthology[match[1]] = match[2];
        }
      } else if (line.startsWith('DBLINKS')) {
        if (!result.dblinks) result.dblinks = {};
        const match = line.match(/DBLINKS\s+(\S+):\s+(.+)/);
        if (match) {
          result.dblinks[match[1]] = match[2].split(/\s+/);
        }
      } else if (line.startsWith('///')) {
        break;
      }
    }

    return result;
  }

  private parseKEGGList(data: string): KEGGListResult {
    const result: KEGGListResult = {};
    const lines = data.split('\n').filter(line => line.trim());

    for (const line of lines) {
      const tabIndex = line.indexOf('\t');
      if (tabIndex > 0) {
        const id = line.substring(0, tabIndex);
        const name = line.substring(tabIndex + 1);
        result[id] = name;
      }
    }

    return result;
  }

  private setupResourceHandlers() {
    // List available resource templates
    this.server.setRequestHandler(
      ListResourceTemplatesRequestSchema,
      async () => ({
        resourceTemplates: [
          {
            uriTemplate: 'kegg://pathway/{pathway_id}',
            name: 'KEGG pathway information',
            mimeType: 'application/json',
            description: 'Complete pathway information including genes, compounds, and reactions',
          },
          {
            uriTemplate: 'kegg://gene/{org}:{gene_id}',
            name: 'KEGG gene entry',
            mimeType: 'application/json',
            description: 'Gene information including sequences, pathways, and orthology',
          },
          {
            uriTemplate: 'kegg://compound/{compound_id}',
            name: 'KEGG compound entry',
            mimeType: 'application/json',
            description: 'Chemical compound information including structure and reactions',
          },
          {
            uriTemplate: 'kegg://reaction/{reaction_id}',
            name: 'KEGG reaction entry',
            mimeType: 'application/json',
            description: 'Biochemical reaction information including equation and enzymes',
          },
          {
            uriTemplate: 'kegg://disease/{disease_id}',
            name: 'KEGG disease entry',
            mimeType: 'application/json',
            description: 'Disease information including associated genes and pathways',
          },
          {
            uriTemplate: 'kegg://drug/{drug_id}',
            name: 'KEGG drug entry',
            mimeType: 'application/json',
            description: 'Drug information including targets and interactions',
          },
          {
            uriTemplate: 'kegg://organism/{org_code}',
            name: 'KEGG organism information',
            mimeType: 'application/json',
            description: 'Organism information and statistics',
          },
          {
            uriTemplate: 'kegg://search/{database}/{query}',
            name: 'KEGG search results',
            mimeType: 'application/json',
            description: 'Search results for the specified database and query',
          },
        ],
      })
    );

    // Handle resource requests
    this.server.setRequestHandler(
      ReadResourceRequestSchema,
      async (request: any) => {
        const uri = request.params.uri;

        // Handle pathway requests
        const pathwayMatch = uri.match(/^kegg:\/\/pathway\/(.+)$/);
        if (pathwayMatch) {
          const pathwayId = pathwayMatch[1];
          try {
            const response = await this.apiClient.get(`/get/${pathwayId}`);
            const pathwayInfo = this.parseKEGGEntry(response.data);
            return {
              contents: [
                {
                  uri: request.params.uri,
                  mimeType: 'application/json',
                  text: JSON.stringify(pathwayInfo, null, 2),
                },
              ],
            };
          } catch (error) {
            throw new McpError(
              ErrorCode.InternalError,
              `Failed to fetch pathway ${pathwayId}: ${error instanceof Error ? error.message : 'Unknown error'}`
            );
          }
        }

        // Handle gene requests
        const geneMatch = uri.match(/^kegg:\/\/gene\/(.+)$/);
        if (geneMatch) {
          const geneId = geneMatch[1];
          try {
            const response = await this.apiClient.get(`/get/${geneId}`);
            const geneInfo = this.parseKEGGEntry(response.data);
            return {
              contents: [
                {
                  uri: request.params.uri,
                  mimeType: 'application/json',
                  text: JSON.stringify(geneInfo, null, 2),
                },
              ],
            };
          } catch (error) {
            throw new McpError(
              ErrorCode.InternalError,
              `Failed to fetch gene ${geneId}: ${error instanceof Error ? error.message : 'Unknown error'}`
            );
          }
        }

        // Handle compound requests
        const compoundMatch = uri.match(/^kegg:\/\/compound\/(.+)$/);
        if (compoundMatch) {
          const compoundId = compoundMatch[1];
          try {
            const response = await this.apiClient.get(`/get/${compoundId}`);
            const compoundInfo = this.parseKEGGEntry(response.data);
            return {
              contents: [
                {
                  uri: request.params.uri,
                  mimeType: 'application/json',
                  text: JSON.stringify(compoundInfo, null, 2),
                },
              ],
            };
          } catch (error) {
            throw new McpError(
              ErrorCode.InternalError,
              `Failed to fetch compound ${compoundId}: ${error instanceof Error ? error.message : 'Unknown error'}`
            );
          }
        }

        // Handle search requests
        const searchMatch = uri.match(/^kegg:\/\/search\/([^\/]+)\/(.+)$/);
        if (searchMatch) {
          const database = searchMatch[1];
          const query = decodeURIComponent(searchMatch[2]);
          try {
            const response = await this.apiClient.get(`/find/${database}/${encodeURIComponent(query)}`);
            const results = this.parseKEGGList(response.data);
            return {
              contents: [
                {
                  uri: request.params.uri,
                  mimeType: 'application/json',
                  text: JSON.stringify({ search_results: results, query, database }, null, 2),
                },
              ],
            };
          } catch (error) {
            throw new McpError(
              ErrorCode.InternalError,
              `Failed to search ${database} for ${query}: ${error instanceof Error ? error.message : 'Unknown error'}`
            );
          }
        }

        throw new McpError(
          ErrorCode.InvalidRequest,
          `Invalid URI format: ${uri}`
        );
      }
    );
  }

  private setupToolHandlers() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [
        // Database Information & Statistics
        {
          name: 'get_database_info',
          description: 'Get release information and statistics for any KEGG database',
          inputSchema: {
            type: 'object',
            properties: {
              database: { type: 'string', description: 'Database name (kegg, pathway, brite, module, ko, genes, genome, compound, glycan, reaction, rclass, enzyme, network, variant, disease, drug, dgroup, or organism code)' },
            },
            required: ['database'],
          },
        },
        {
          name: 'list_organisms',
          description: 'Get all KEGG organisms with codes and names',
          inputSchema: {
            type: 'object',
            properties: {
              limit: { type: 'number', description: 'Maximum number of organisms to return (default: 100)', minimum: 1, maximum: 1000 },
            },
            required: [],
          },
        },

        // Pathway Analysis
        {
          name: 'search_pathways',
          description: 'Search pathways by keywords or pathway names',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (pathway name, keywords, or description)' },
              organism_code: { type: 'string', description: 'Organism code to filter results (optional, e.g., hsa, mmu, eco)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_pathway_info',
          description: 'Get detailed information for a specific pathway',
          inputSchema: {
            type: 'object',
            properties: {
              pathway_id: { type: 'string', description: 'Pathway ID (e.g., map00010, hsa00010, ko00010)' },
              format: { type: 'string', enum: ['json', 'kgml', 'image', 'conf'], description: 'Output format (default: json)' },
            },
            required: ['pathway_id'],
          },
        },
        {
          name: 'get_pathway_genes',
          description: 'Get all genes involved in a specific pathway',
          inputSchema: {
            type: 'object',
            properties: {
              pathway_id: { type: 'string', description: 'Pathway ID (e.g., hsa00010, mmu00010)' },
            },
            required: ['pathway_id'],
          },
        },

        // Gene Analysis
        {
          name: 'search_genes',
          description: 'Search genes by name, symbol, or keywords',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (gene name, symbol, or keywords)' },
              organism_code: { type: 'string', description: 'Organism code to filter results (optional, e.g., hsa, mmu)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_gene_info',
          description: 'Get detailed information for a specific gene',
          inputSchema: {
            type: 'object',
            properties: {
              gene_id: { type: 'string', description: 'Gene ID (e.g., hsa:1956, mmu:11651, eco:b0008)' },
              include_sequences: { type: 'boolean', description: 'Include amino acid and nucleotide sequences (default: false)' },
            },
            required: ['gene_id'],
          },
        },

        // Compound Analysis
        {
          name: 'search_compounds',
          description: 'Search compounds by name, formula, or chemical structure',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (compound name, formula, or identifier)' },
              search_type: { type: 'string', enum: ['name', 'formula', 'exact_mass', 'mol_weight'], description: 'Type of search (default: name)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_compound_info',
          description: 'Get detailed information for a specific compound',
          inputSchema: {
            type: 'object',
            properties: {
              compound_id: { type: 'string', description: 'Compound ID (e.g., C00002, C00031, cpd:C00002)' },
            },
            required: ['compound_id'],
          },
        },

        // Reaction & Enzyme Analysis
        {
          name: 'search_reactions',
          description: 'Search biochemical reactions by keywords or reaction components',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (reaction name, enzyme, or compound)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_reaction_info',
          description: 'Get detailed information for a specific reaction',
          inputSchema: {
            type: 'object',
            properties: {
              reaction_id: { type: 'string', description: 'Reaction ID (e.g., R00001, R00002)' },
            },
            required: ['reaction_id'],
          },
        },
        {
          name: 'search_enzymes',
          description: 'Search enzymes by EC number or enzyme name',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (EC number or enzyme name)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_enzyme_info',
          description: 'Get detailed enzyme information by EC number',
          inputSchema: {
            type: 'object',
            properties: {
              ec_number: { type: 'string', description: 'EC number (e.g., ec:1.1.1.1)' },
            },
            required: ['ec_number'],
          },
        },

        // Disease & Drug Analysis
        {
          name: 'search_diseases',
          description: 'Search human diseases by name or keywords',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (disease name or keywords)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_disease_info',
          description: 'Get detailed information for a specific disease',
          inputSchema: {
            type: 'object',
            properties: {
              disease_id: { type: 'string', description: 'Disease ID (e.g., H00001, H00002)' },
            },
            required: ['disease_id'],
          },
        },
        {
          name: 'search_drugs',
          description: 'Search drugs by name, target, or indication',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (drug name, target, or indication)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_drug_info',
          description: 'Get detailed information for a specific drug',
          inputSchema: {
            type: 'object',
            properties: {
              drug_id: { type: 'string', description: 'Drug ID (e.g., D00001, D00002)' },
            },
            required: ['drug_id'],
          },
        },
        {
          name: 'get_drug_interactions',
          description: 'Find adverse drug-drug interactions',
          inputSchema: {
            type: 'object',
            properties: {
              drug_ids: { type: 'array', items: { type: 'string' }, description: 'Drug IDs to check for interactions (1-10)', minItems: 1, maxItems: 10 },
            },
            required: ['drug_ids'],
          },
        },

        // Module & Orthology Analysis
        {
          name: 'search_modules',
          description: 'Search KEGG modules by name or function',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (module name or function)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_module_info',
          description: 'Get detailed information for a specific module',
          inputSchema: {
            type: 'object',
            properties: {
              module_id: { type: 'string', description: 'Module ID (e.g., M00001, M00002)' },
            },
            required: ['module_id'],
          },
        },
        {
          name: 'search_ko_entries',
          description: 'Search KEGG Orthology entries by function or gene name',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (function or gene name)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_ko_info',
          description: 'Get detailed information for a specific KO entry',
          inputSchema: {
            type: 'object',
            properties: {
              ko_id: { type: 'string', description: 'KO ID (e.g., K00001, K00002)' },
            },
            required: ['ko_id'],
          },
        },

        // Glycan Analysis
        {
          name: 'search_glycans',
          description: 'Search glycan structures by name or composition',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (glycan name or composition)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_glycan_info',
          description: 'Get detailed information for a specific glycan',
          inputSchema: {
            type: 'object',
            properties: {
              glycan_id: { type: 'string', description: 'Glycan ID (e.g., G00001, G00002)' },
            },
            required: ['glycan_id'],
          },
        },

        // BRITE Hierarchy Analysis
        {
          name: 'search_brite',
          description: 'Search BRITE functional hierarchies',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Search query (function or category)' },
              hierarchy_type: { type: 'string', enum: ['br', 'ko', 'jp'], description: 'Type of BRITE hierarchy (default: br)' },
              max_results: { type: 'number', description: 'Maximum number of results (1-1000, default: 50)', minimum: 1, maximum: 1000 },
            },
            required: ['query'],
          },
        },
        {
          name: 'get_brite_info',
          description: 'Get detailed information for a specific BRITE entry',
          inputSchema: {
            type: 'object',
            properties: {
              brite_id: { type: 'string', description: 'BRITE ID (e.g., br:br08301, ko:K00001)' },
              format: { type: 'string', enum: ['json', 'htext'], description: 'Output format (default: json)' },
            },
            required: ['brite_id'],
          },
        },

        // Advanced Analysis Tools
        {
          name: 'get_pathway_compounds',
          description: 'Get all compounds involved in a specific pathway',
          inputSchema: {
            type: 'object',
            properties: {
              pathway_id: { type: 'string', description: 'Pathway ID (e.g., map00010, hsa00010)' },
            },
            required: ['pathway_id'],
          },
        },
        {
          name: 'get_pathway_reactions',
          description: 'Get all reactions involved in a specific pathway',
          inputSchema: {
            type: 'object',
            properties: {
              pathway_id: { type: 'string', description: 'Pathway ID (e.g., map00010, rn00010)' },
            },
            required: ['pathway_id'],
          },
        },
        {
          name: 'get_compound_reactions',
          description: 'Get all reactions involving a specific compound',
          inputSchema: {
            type: 'object',
            properties: {
              compound_id: { type: 'string', description: 'Compound ID (e.g., C00002, C00031)' },
            },
            required: ['compound_id'],
          },
        },
        {
          name: 'get_gene_orthologs',
          description: 'Find orthologous genes across organisms',
          inputSchema: {
            type: 'object',
            properties: {
              gene_id: { type: 'string', description: 'Gene ID (e.g., hsa:1956)' },
              target_organisms: { type: 'array', items: { type: 'string' }, description: 'Target organism codes (optional, e.g., [mmu, rno, dme])' },
            },
            required: ['gene_id'],
          },
        },
        {
          name: 'batch_entry_lookup',
          description: 'Process multiple KEGG entries efficiently',
          inputSchema: {
            type: 'object',
            properties: {
              entry_ids: { type: 'array', items: { type: 'string' }, description: 'KEGG entry IDs (1-50)', minItems: 1, maxItems: 50 },
              operation: { type: 'string', enum: ['info', 'sequence', 'pathway', 'link'], description: 'Operation to perform (default: info)' },
            },
            required: ['entry_ids'],
          },
        },

        // Cross-References & Integration
        {
          name: 'convert_identifiers',
          description: 'Convert between KEGG and external database identifiers',
          inputSchema: {
            type: 'object',
            properties: {
              source_db: { type: 'string', description: 'Source database (e.g., hsa, ncbi-geneid, uniprot)' },
              target_db: { type: 'string', description: 'Target database (e.g., hsa, ncbi-geneid, uniprot)' },
              identifiers: { type: 'array', items: { type: 'string' }, description: 'Identifiers to convert (optional, for batch conversion)' },
            },
            required: ['source_db', 'target_db'],
          },
        },
        {
          name: 'find_related_entries',
          description: 'Find related entries across KEGG databases using cross-references',
          inputSchema: {
            type: 'object',
            properties: {
              source_db: { type: 'string', description: 'Source database (e.g., pathway, compound, gene)' },
              target_db: { type: 'string', description: 'Target database (e.g., pathway, compound, gene)' },
              source_entries: { type: 'array', items: { type: 'string' }, description: 'Source entries to find links for (optional)' },
            },
            required: ['source_db', 'target_db'],
          },
        },
      ],
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request: any) => {
      const { name, arguments: args } = request.params;

      try {
        switch (name) {
          // Database Information
          case 'get_database_info':
            return await this.handleGetDatabaseInfo(args);
          case 'list_organisms':
            return await this.handleListOrganisms(args);

          // Pathway Analysis
          case 'search_pathways':
            return await this.handleSearchPathways(args);
          case 'get_pathway_info':
            return await this.handleGetPathwayInfo(args);
          case 'get_pathway_genes':
            return await this.handleGetPathwayGenes(args);

          // Gene Analysis
          case 'search_genes':
            return await this.handleSearchGenes(args);
          case 'get_gene_info':
            return await this.handleGetGeneInfo(args);

          // Compound Analysis
          case 'search_compounds':
            return await this.handleSearchCompounds(args);
          case 'get_compound_info':
            return await this.handleGetCompoundInfo(args);

          // Reaction & Enzyme Analysis
          case 'search_reactions':
            return await this.handleSearchReactions(args);
          case 'get_reaction_info':
            return await this.handleGetReactionInfo(args);
          case 'search_enzymes':
            return await this.handleSearchEnzymes(args);
          case 'get_enzyme_info':
            return await this.handleGetEnzymeInfo(args);

          // Disease & Drug Analysis
          case 'search_diseases':
            return await this.handleSearchDiseases(args);
          case 'get_disease_info':
            return await this.handleGetDiseaseInfo(args);
          case 'search_drugs':
            return await this.handleSearchDrugs(args);
          case 'get_drug_info':
            return await this.handleGetDrugInfo(args);
          case 'get_drug_interactions':
            return await this.handleGetDrugInteractions(args);

          // Module & Orthology Analysis
          case 'search_modules':
            return await this.handleSearchModules(args);
          case 'get_module_info':
            return await this.handleGetModuleInfo(args);
          case 'search_ko_entries':
            return await this.handleSearchKoEntries(args);
          case 'get_ko_info':
            return await this.handleGetKoInfo(args);

          // Glycan Analysis
          case 'search_glycans':
            return await this.handleSearchGlycans(args);
          case 'get_glycan_info':
            return await this.handleGetGlycanInfo(args);

          // BRITE Hierarchy Analysis
          case 'search_brite':
            return await this.handleSearchBrite(args);
          case 'get_brite_info':
            return await this.handleGetBriteInfo(args);

          // Advanced Analysis Tools
          case 'get_pathway_compounds':
            return await this.handleGetPathwayCompounds(args);
          case 'get_pathway_reactions':
            return await this.handleGetPathwayReactions(args);
          case 'get_compound_reactions':
            return await this.handleGetCompoundReactions(args);
          case 'get_gene_orthologs':
            return await this.handleGetGeneOrthologs(args);
          case 'batch_entry_lookup':
            return await this.handleBatchEntryLookup(args);

          // Cross-References
          case 'convert_identifiers':
            return await this.handleConvertIdentifiers(args);
          case 'find_related_entries':
            return await this.handleFindRelatedEntries(args);

          default:
            throw new McpError(
              ErrorCode.MethodNotFound,
              `Unknown tool: ${name}`
            );
        }
      } catch (error) {
        return {
          content: [
            {
              type: 'text',
              text: `Error executing tool ${name}: ${error instanceof Error ? error.message : 'Unknown error'}`,
            },
          ],
          isError: true,
        };
      }
    });
  }

  // Tool handler implementations
  private async handleGetDatabaseInfo(args: any) {
    if (!args.database || typeof args.database !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Database parameter is required');
    }

    try {
      const response = await this.apiClient.get(`/info/${args.database}`);
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              database: args.database,
              info: response.data,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get database info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleListOrganisms(args: any) {
    if (!isValidOrganismArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid organism arguments');
    }

    try {
      const response = await this.apiClient.get('/list/organism');
      const organisms = this.parseKEGGList(response.data);
      const limit = args.limit || 100;

      const limitedOrganisms = Object.fromEntries(
        Object.entries(organisms).slice(0, limit)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              total_organisms: Object.keys(organisms).length,
              returned_count: Object.keys(limitedOrganisms).length,
              organisms: limitedOrganisms,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to list organisms: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchPathways(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const database = args.organism_code ? `pathway/${args.organism_code}` : 'pathway';
      const response = await this.apiClient.get(`/find/${database}/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              database: database,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              pathways: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search pathways: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetPathwayInfo(args: any) {
    if (!isValidPathwayInfoArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid pathway arguments');
    }

    try {
      const format = args.format || 'json';
      let endpoint = `/get/${args.pathway_id}`;

      if (format !== 'json') {
        endpoint += `/${format}`;
      }

      const response = await this.apiClient.get(endpoint);

      if (format === 'json') {
        const pathwayInfo = this.parseKEGGEntry(response.data);
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(pathwayInfo, null, 2),
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: 'text',
              text: response.data,
            },
          ],
        };
      }
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get pathway info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetPathwayGenes(args: any) {
    if (!isValidPathwayArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid pathway arguments');
    }

    try {
      const response = await this.apiClient.get(`/link/genes/${args.pathway_id}`);
      const geneLinks = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              pathway_id: args.pathway_id,
              gene_count: Object.keys(geneLinks).length,
              genes: geneLinks,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get pathway genes: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchGenes(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const database = args.organism_code || 'genes';
      const response = await this.apiClient.get(`/find/${database}/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              database: database,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              genes: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search genes: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetGeneInfo(args: any) {
    if (!isValidGeneArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid gene arguments');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.gene_id}`);
      const geneInfo = this.parseKEGGEntry(response.data);

      if (args.include_sequences) {
        try {
          const [aaseqResponse, ntseqResponse] = await Promise.all([
            this.apiClient.get(`/get/${args.gene_id}/aaseq`).catch(() => ({ data: null })),
            this.apiClient.get(`/get/${args.gene_id}/ntseq`).catch(() => ({ data: null })),
          ]);

          if (aaseqResponse.data) {
            geneInfo.aaseq = aaseqResponse.data;
          }
          if (ntseqResponse.data) {
            geneInfo.ntseq = ntseqResponse.data;
          }
        } catch (error) {
          // Sequences not available, continue without them
        }
      }

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(geneInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get gene info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchCompounds(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const searchType = args.search_type || 'name';
      let endpoint = '';

      if (searchType === 'formula') {
        endpoint = `/find/compound/${encodeURIComponent(args.query)}/formula`;
      } else if (searchType === 'exact_mass') {
        endpoint = `/find/compound/${encodeURIComponent(args.query)}/exact_mass`;
      } else if (searchType === 'mol_weight') {
        endpoint = `/find/compound/${encodeURIComponent(args.query)}/mol_weight`;
      } else {
        endpoint = `/find/compound/${encodeURIComponent(args.query)}`;
      }

      const response = await this.apiClient.get(endpoint);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              search_type: searchType,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              compounds: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search compounds: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetCompoundInfo(args: any) {
    if (!args.compound_id || typeof args.compound_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Compound ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.compound_id}`);
      const compoundInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(compoundInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get compound info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleConvertIdentifiers(args: any) {
    if (!args.source_db || !args.target_db) {
      throw new McpError(ErrorCode.InvalidParams, 'Source and target databases are required');
    }

    try {
      let endpoint = '';

      if (args.identifiers && args.identifiers.length > 0) {
        // Convert specific identifiers
        const identifierList = args.identifiers.join('+');
        endpoint = `/conv/${args.target_db}/${identifierList}`;
      } else {
        // Get all conversions between databases
        endpoint = `/conv/${args.target_db}/${args.source_db}`;
      }

      const response = await this.apiClient.get(endpoint);
      const conversions = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              source_db: args.source_db,
              target_db: args.target_db,
              conversion_count: Object.keys(conversions).length,
              conversions: conversions,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to convert identifiers: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleFindRelatedEntries(args: any) {
    if (!args.source_db || !args.target_db) {
      throw new McpError(ErrorCode.InvalidParams, 'Source and target databases are required');
    }

    try {
      let endpoint = '';

      if (args.source_entries && args.source_entries.length > 0) {
        // Find links for specific entries
        const entryList = args.source_entries.join('+');
        endpoint = `/link/${args.target_db}/${entryList}`;
      } else {
        // Get all links between databases
        endpoint = `/link/${args.target_db}/${args.source_db}`;
      }

      const response = await this.apiClient.get(endpoint);
      const links = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              source_db: args.source_db,
              target_db: args.target_db,
              link_count: Object.keys(links).length,
              links: links,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to find related entries: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  // Additional handler implementations
  private async handleSearchReactions(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/reaction/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              reactions: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search reactions: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetReactionInfo(args: any) {
    if (!args.reaction_id || typeof args.reaction_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Reaction ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.reaction_id}`);
      const reactionInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(reactionInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get reaction info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchEnzymes(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/enzyme/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              enzymes: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search enzymes: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetEnzymeInfo(args: any) {
    if (!args.ec_number || typeof args.ec_number !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'EC number is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.ec_number}`);
      const enzymeInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(enzymeInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get enzyme info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchDiseases(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/disease/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              diseases: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search diseases: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetDiseaseInfo(args: any) {
    if (!args.disease_id || typeof args.disease_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Disease ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.disease_id}`);
      const diseaseInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(diseaseInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get disease info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchDrugs(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/drug/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              drugs: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search drugs: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetDrugInfo(args: any) {
    if (!args.drug_id || typeof args.drug_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Drug ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.drug_id}`);
      const drugInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(drugInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get drug info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetDrugInteractions(args: any) {
    if (!args.drug_ids || !Array.isArray(args.drug_ids) || args.drug_ids.length === 0) {
      throw new McpError(ErrorCode.InvalidParams, 'Drug IDs array is required');
    }

    try {
      const drugList = args.drug_ids.join('+');
      const response = await this.apiClient.get(`/ddi/${drugList}`);
      const interactions = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              drug_ids: args.drug_ids,
              interaction_count: Object.keys(interactions).length,
              interactions: interactions,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get drug interactions: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchModules(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/module/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              modules: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search modules: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetModuleInfo(args: any) {
    if (!args.module_id || typeof args.module_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Module ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.module_id}`);
      const moduleInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(moduleInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get module info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchKoEntries(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/ko/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              ko_entries: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search KO entries: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetKoInfo(args: any) {
    if (!args.ko_id || typeof args.ko_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'KO ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.ko_id}`);
      const koInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(koInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get KO info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchGlycans(args: any) {
    if (!isValidSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const response = await this.apiClient.get(`/find/glycan/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              glycans: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search glycans: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetGlycanInfo(args: any) {
    if (!args.glycan_id || typeof args.glycan_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Glycan ID is required');
    }

    try {
      const response = await this.apiClient.get(`/get/${args.glycan_id}`);
      const glycanInfo = this.parseKEGGEntry(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(glycanInfo, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get glycan info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleSearchBrite(args: any) {
    if (!isValidBriteSearchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid search arguments');
    }

    try {
      const hierarchyType = args.hierarchy_type || 'br';
      const response = await this.apiClient.get(`/find/brite/${encodeURIComponent(args.query)}`);
      const results = this.parseKEGGList(response.data);
      const maxResults = args.max_results || 50;

      const limitedResults = Object.fromEntries(
        Object.entries(results).slice(0, maxResults)
      );

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              query: args.query,
              hierarchy_type: hierarchyType,
              total_found: Object.keys(results).length,
              returned_count: Object.keys(limitedResults).length,
              brite_entries: limitedResults,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to search BRITE: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetBriteInfo(args: any) {
    if (!args.brite_id || typeof args.brite_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'BRITE ID is required');
    }

    try {
      const format = args.format || 'json';
      let endpoint = `/get/${args.brite_id}`;

      if (format === 'htext') {
        endpoint += '/htext';
      }

      const response = await this.apiClient.get(endpoint);

      if (format === 'json') {
        const briteInfo = this.parseKEGGEntry(response.data);
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(briteInfo, null, 2),
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: 'text',
              text: response.data,
            },
          ],
        };
      }
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get BRITE info: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetPathwayCompounds(args: any) {
    if (!args.pathway_id || typeof args.pathway_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Pathway ID is required');
    }

    try {
      const response = await this.apiClient.get(`/link/compound/${args.pathway_id}`);
      const compoundLinks = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              pathway_id: args.pathway_id,
              compound_count: Object.keys(compoundLinks).length,
              compounds: compoundLinks,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get pathway compounds: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetPathwayReactions(args: any) {
    if (!args.pathway_id || typeof args.pathway_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Pathway ID is required');
    }

    try {
      const response = await this.apiClient.get(`/link/reaction/${args.pathway_id}`);
      const reactionLinks = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              pathway_id: args.pathway_id,
              reaction_count: Object.keys(reactionLinks).length,
              reactions: reactionLinks,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get pathway reactions: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetCompoundReactions(args: any) {
    if (!args.compound_id || typeof args.compound_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Compound ID is required');
    }

    try {
      const response = await this.apiClient.get(`/link/reaction/${args.compound_id}`);
      const reactionLinks = this.parseKEGGList(response.data);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              compound_id: args.compound_id,
              reaction_count: Object.keys(reactionLinks).length,
              reactions: reactionLinks,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get compound reactions: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleGetGeneOrthologs(args: any) {
    if (!args.gene_id || typeof args.gene_id !== 'string') {
      throw new McpError(ErrorCode.InvalidParams, 'Gene ID is required');
    }

    try {
      const response = await this.apiClient.get(`/link/ko/${args.gene_id}`);
      const koLinks = this.parseKEGGList(response.data);

      // If target organisms specified, filter results
      let orthologs = koLinks;
      if (args.target_organisms && Array.isArray(args.target_organisms)) {
        // Get genes for each KO in target organisms
        const orthologResults: any = {};
        for (const ko of Object.keys(koLinks)) {
          for (const org of args.target_organisms) {
            try {
              const orgResponse = await this.apiClient.get(`/link/${org}/${ko}`);
              const orgGenes = this.parseKEGGList(orgResponse.data);
              Object.assign(orthologResults, orgGenes);
            } catch (error) {
              // Continue if organism doesn't have this KO
            }
          }
        }
        orthologs = orthologResults;
      }

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              gene_id: args.gene_id,
              target_organisms: args.target_organisms,
              ortholog_count: Object.keys(orthologs).length,
              orthologs: orthologs,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Failed to get gene orthologs: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  private async handleBatchEntryLookup(args: any) {
    if (!isValidBatchArgs(args)) {
      throw new McpError(ErrorCode.InvalidParams, 'Invalid batch arguments');
    }

    try {
      const operation = args.operation || 'info';
      const results = [];

      for (const entryId of args.entry_ids) {
        try {
          let response;
          switch (operation) {
            case 'sequence':
              response = await this.apiClient.get(`/get/${entryId}/aaseq`);
              break;
            case 'pathway':
              response = await this.apiClient.get(`/link/pathway/${entryId}`);
              break;
            case 'link':
              response = await this.apiClient.get(`/link/ko/${entryId}`);
              break;
            default:
              response = await this.apiClient.get(`/get/${entryId}`);
          }

          if (operation === 'info') {
            const entryInfo = this.parseKEGGEntry(response.data);
            results.push({ entry_id: entryId, data: entryInfo, success: true });
          } else {
            const linkData = this.parseKEGGList(response.data);
            results.push({ entry_id: entryId, data: linkData, success: true });
          }
        } catch (error) {
          results.push({
            entry_id: entryId,
            error: error instanceof Error ? error.message : 'Unknown error',
            success: false,
          });
        }
      }

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              operation: operation,
              total_entries: args.entry_ids.length,
              successful: results.filter(r => r.success).length,
              failed: results.filter(r => !r.success).length,
              results: results,
            }, null, 2),
          },
        ],
      };
    } catch (error) {
      throw new McpError(
        ErrorCode.InternalError,
        `Batch lookup failed: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  async run() {
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    console.error('KEGG MCP server running on stdio');
  }
}

const server = new KEGGServer();
server.run().catch(console.error);

export interface ItemTemplate {
  type: string;
  label: string;
  hint: string;
  keyPrefix: string;
  /** "markdown": body is markdown, extra fields live in meta (JSON). "yaml": body is YAML. */
  format: "markdown" | "yaml";
  body: string;
  meta: Record<string, unknown>;
}

export const TEMPLATES: ItemTemplate[] = [
  {
    type: "guide",
    label: "Guide",
    hint: "How-to knowledge agents should read before answering a class of question.",
    keyPrefix: "guides/",
    format: "markdown",
    body: "# Title\n\nWhen to use this guide, then the steps and the pitfalls.\n",
    meta: { name: "New guide", description: "One line on when an agent should read this", tags: [] },
  },
  {
    type: "workspace_rule",
    label: "Workspace rule",
    hint: "A standing rule applied to every question in this workspace.",
    keyPrefix: "rules/",
    format: "markdown",
    body: "Always state the time zone and currency of any figure you report.\n",
    meta: {},
  },
  {
    type: "glossary_term",
    label: "Glossary term",
    hint: "A business term with synonyms and a definition.",
    keyPrefix: "glossary/",
    format: "yaml",
    body: "term: Active customer\nsynonyms:\n  - live customer\ndefinition: >-\n  A customer with at least one completed order in the last 90 days.\nrelated_assets: []\n",
    meta: {},
  },
  {
    type: "golden_query",
    label: "Golden query",
    hint: "A verified question with the SQL that answers it.",
    keyPrefix: "queries/",
    format: "yaml",
    body: "question: What was revenue last month?\nvariants:\n  - monthly revenue\nconnection: jaffle\nsql: |\n  select 1\nverified_by: you@example.com\n",
    meta: {},
  },
  {
    type: "asset_doc",
    label: "Asset doc",
    hint: "Documentation for a table or view: grain, caveats, columns.",
    keyPrefix: "assets/",
    format: "yaml",
    body: "asset: analytics.orders\ndescription: One row per order.\ngrain: order\ncaveats: []\nendorsed: false\ncolumns:\n  - name: id\n    description: Primary key\n",
    meta: {},
  },
  {
    type: "endorsement",
    label: "Endorsement",
    hint: "Mark an asset as endorsed, or exclude it from agents.",
    keyPrefix: "endorsements/",
    format: "yaml",
    body: "asset: analytics.orders\nendorsed: true\nexcluded_from_ai: false\nreason: Source of truth for revenue.\n",
    meta: {},
  },
  {
    type: "caveat",
    label: "Caveat",
    hint: "A known data quality issue, scoped to assets and optionally dates.",
    keyPrefix: "caveats/",
    format: "markdown",
    body: "Refunds before 2023 were booked as negative orders.\n",
    meta: { applies_to: ["analytics.orders"] },
  },
  {
    type: "semantic_model",
    label: "Semantic model",
    hint: "An ossie or wren MDL model definition.",
    keyPrefix: "semantic/",
    format: "markdown",
    body: "models: []\n",
    meta: { format: "ossie" },
  },
];

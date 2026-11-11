cube(`legacy_refunds`, {
  sql_table: `analytics.payments`,
  description: `Refunds issued by the old checkout. Defined in a JavaScript model.`,

  dimensions: {
    payment_id: { sql: `payment_id`, type: `number`, primary_key: true },
    reason: {
      sql: `payment_method`,
      type: `string`,
      description: `How the refunded payment was made.`,
    },
  },

  measures: {
    refunded_total: {
      sql: `amount / 100.0`,
      type: `sum`,
      description: `Total refunded through the old checkout, in AUD dollars.`,
      filters: [{ sql: `${CUBE}.status = 'refunded'` }],
    },
    refund_count: { type: `count` },
  },
});

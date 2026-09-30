/**
 * A fake DSH/Cordis-style bundle used to exercise bridges/dsh-host.mjs.
 * Exposes the "tools map" shape the bridge knows how to harvest.
 */

export const tools = {
  greet: {
    name: 'greet',
    description: 'Greet someone by name.',
    inputSchema: {
      type: 'object',
      properties: { who: { type: 'string' } },
      required: ['who'],
    },
    async invoke(args) {
      return `hello ${args.who || 'world'}`;
    },
  },
  tally: {
    description: 'Sum a list of numbers.',
    inputSchema: {
      type: 'object',
      properties: { values: { type: 'array', items: { type: 'number' } } },
      required: ['values'],
    },
    invoke(args) {
      const values = Array.isArray(args.values) ? args.values : [];
      return String(values.reduce((a, b) => a + Number(b || 0), 0));
    },
  },
};

export const notATool = 'this string must be ignored';

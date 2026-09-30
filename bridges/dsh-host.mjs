#!/usr/bin/env node
/**
 * dsh-host.mjs — optional DSH / Cordis bridge for Tacit.
 *
 * Node is NOT a dependency of Tacit. This file only runs if you ask for it, and
 * only if you already have Node installed.
 *
 * It loads a DSH/Cordis bundle, introspects whatever it exports for
 * tool-shaped values, and re-exposes them as an MCP server over stdio — which
 * is exactly what Tacit's MCP client speaks. Nothing is installed, nothing is
 * fetched, and nothing runs until you point this at something yourself.
 *
 *   node bridges/dsh-host.mjs --plugin @scope/some-dsh-plugin
 *   node bridges/dsh-host.mjs --plugin ./local/bundle.mjs
 *   node bridges/dsh-host.mjs --plugin @scope/x --list     # just print tools
 *
 * Register it in Tacit as an MCP server:
 *   command : node
 *   args    : <path to this file> --plugin <the bundle>
 *
 * Expectation setting: bundles that expose a clean tool table load directly.
 * Bundles that expect the full Cordis runtime will not — the script says so
 * plainly on stderr and exits non-zero, rather than pretending to work.
 */

import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import path from 'node:path';

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_INFO = { name: 'dsh-host', version: '0.1' };

function arg(name, fallback = '') {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const pluginSpec = arg('--plugin') || arg('--package');
const listOnly = process.argv.includes('--list');
const verbose = process.argv.includes('--verbose');

function log(...parts) {
  if (verbose) process.stderr.write('[dsh-host] ' + parts.join(' ') + '\n');
}

function send(payload) {
  process.stdout.write(JSON.stringify(payload) + '\n');
}

function describeError(err) {
  return err && err.stack ? String(err.stack).split('\n').slice(0, 3).join(' | ') : String(err);
}

/* ── loading ────────────────────────────────────────────────────────────── */

async function load(spec) {
  const abs = path.resolve(spec);
  // A path wins over a package name: relative/absolute forms, JS extensions, or
  // anything that actually exists on disk. `@scope/pkg` is left alone because
  // it has none of those and does not resolve here.
  const looksLikePath = spec.startsWith('.') || spec.startsWith('/')
    || /^[A-Za-z]:[\\/]/.test(spec)
    || /\.(mjs|cjs|js)$/i.test(spec)
    || existsSync(abs);
  if (looksLikePath) {
    if (!existsSync(abs)) throw new Error(`no such file: ${abs}`);
    return import(pathToFileURL(abs).href);
  }
  try {
    return await import(spec);                 // ESM package
  } catch (esmErr) {
    log('ESM import failed, trying CommonJS:', describeError(esmErr));
    const require = createRequire(import.meta.url);
    return require(spec);                      // CommonJS package
  }
}

/* Accept the several shapes a bundle might use for its tools. */
function normalize(raw, name) {
  const entry = raw || {};
  const fn = typeof entry === 'function' ? entry : (entry.invoke || entry.execute
    || entry.handler || entry.run || entry.call);
  return {
    name: entry.name || name,
    description: entry.description || entry.desc || '',
    inputSchema: entry.inputSchema || entry.parameters || entry.schema
      || { type: 'object', properties: {} },
    invoke: typeof fn === 'function' ? fn : null,
    raw: entry,
  };
}

function harvest(mod) {
  const sources = [mod.tools, mod.default?.tools, mod.default, mod];
  const out = new Map();
  for (const source of sources) {
    if (!source) continue;
    if (Array.isArray(source)) {
      for (const item of source) {
        const t = normalize(item, item?.name);
        if (t.name) out.set(t.name, t);
      }
      continue;
    }
    if (typeof source !== 'object') continue;
    for (const [key, value] of Object.entries(source)) {
      if (typeof key !== 'string' || key.startsWith('_')) continue;
      if (typeof value !== 'function' && typeof value !== 'object') continue;
      const t = normalize(value, key);
      // only keep things that actually look callable
      if (t.name && t.invoke) out.set(t.name, t);
    }
  }
  return [...out.values()];
}

/* ── MCP ────────────────────────────────────────────────────────────────── */

let TOOLS = [];
let MODULE = null;

async function invoke(tool, args) {
  const t = TOOLS.find((x) => x.name === tool);
  if (!t) throw new Error(`unknown tool ${tool}`);
  /**
   * Cordis-style handlers often take a context object. We pass the arguments
   * directly, and give the handler a second `ctx` argument it may ignore.
   */
  const ctx = { args, module: MODULE, host: 'tacit' };
  const result = await Promise.resolve(t.invoke(args || {}, ctx));
  if (result == null) return '(no output)';
  return typeof result === 'string' ? result : JSON.stringify(result, null, 2);
}

function handle(msg) {
  const method = msg.method;
  const id = msg.id;

  if (method === 'initialize') {
    send({ jsonrpc: '2.0', id, result: {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: {} },
      serverInfo: SERVER_INFO,
    } });
    return;
  }
  if (method === 'notifications/initialized') return;

  if (method === 'tools/list') {
    send({ jsonrpc: '2.0', id, result: {
      tools: TOOLS.map((t) => ({
        name: t.name,
        description: t.description || `Bridged from ${pluginSpec}`,
        inputSchema: t.inputSchema,
      })),
    } });
    return;
  }

  if (method === 'tools/call') {
    const params = msg.params || {};
    invoke(params.name, params.arguments).then(
      (text) => send({ jsonrpc: '2.0', id, result: {
        content: [{ type: 'text', text: String(text) }] } }),
      (err) => send({ jsonrpc: '2.0', id, result: {
        content: [{ type: 'text', text: `error: ${err.message || err}` }], isError: true } }),
    );
    return;
  }

  if (id != null) {
    send({ jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } });
  }
}

async function main() {
  if (!pluginSpec) {
    process.stderr.write(
      'dsh-host: nothing to do.\n' +
      '  usage: node dsh-host.mjs --plugin <npm-package | ./bundle.mjs> [--list]\n');
    process.exit(2);
  }

  try {
    MODULE = await load(pluginSpec);
  } catch (err) {
    process.stderr.write(
      `dsh-host: could not load "${pluginSpec}".\n` +
      `  ${describeError(err)}\n` +
      '  This bundle probably needs the full Cordis runtime. Use Tacit\'s\n' +
      '  "Generate adapter" action to scaffold a hand-written adapter instead.\n');
    process.exit(2);
  }

  TOOLS = harvest(MODULE);
  if (!TOOLS.length) {
    process.stderr.write(
      `dsh-host: loaded "${pluginSpec}" but found no tool-shaped exports.\n` +
      '  Expected a `tools` map/array, or functions with a name and description.\n' +
      '  Generate an adapter scaffold to map it by hand.\n');
    process.exit(3);
  }

  if (listOnly) {
    for (const t of TOOLS) process.stdout.write(`${t.name}\t${t.description}\n`);
    return;
  }

  process.stderr.write(`dsh-host: exposing ${TOOLS.length} tool(s) from ${pluginSpec}\n`);
  let buffer = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => {
    buffer += chunk;
    let idx;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        handle(JSON.parse(line));
      } catch (err) {
        log('bad message:', describeError(err));
      }
    }
  });
}

main().catch((err) => {
  process.stderr.write(`dsh-host: fatal: ${describeError(err)}\n`);
  process.exit(1);
});

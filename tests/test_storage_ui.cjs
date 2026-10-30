const {test} = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

function harness() {
  const elements = new Map();
  function element() {
    return {
      hidden: true, textContent: "", children: [], value: "",
      addEventListener() {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
    };
  }
  const context = vm.createContext({
    URLSearchParams,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
      querySelectorAll: () => [],
    },
    fetch: async () => { throw new Error("Denied"); },
  });
  vm.runInContext(fs.readFileSync("src/s3_mcp_agent/static/app.js", "utf8"), context);
  return {context, elements, run: (source) => vm.runInContext(source, context)};
}

test("scan aggregates pages exactly using bigint and caps extension groups", () => {
  const h = harness();
  h.run(`scan = {objects: 0n, bytes: 9007199254740991n, markers: 0n, pages: 0, token: null, types: new Map()};
    mergeScanPage(scan, {page_totals: {objects: 2, bytes: 9, folder_markers: 1,
      types: {".csv": {objects: 1, bytes: 9}, "folder-marker": {objects: 1, bytes: 0}}},
      next_continuation_token: "next"});
    mergeScanPage(scan, {page_totals: {objects: 1, bytes: 3, folder_markers: 0,
      types: {".csv": {objects: 1, bytes: 3}}}, next_continuation_token: null});`);
  assert.equal(h.run("scan.bytes"), 9007199254741003n);
  assert.equal(h.run("scan.objects"), 3n);
  assert.equal(h.run("scan.types.get('.csv').bytes"), 12n);
  assert.equal(h.run("scan.token"), null);
  h.run(`for (let i=0; i<2000; i++) mergeScanPage(scan, {
    page_totals: {objects: 1, bytes: 1, folder_markers: 0,
      types: {[".type" + i]: {objects: 1, bytes: 1}}}, next_continuation_token: "x"});`);
  assert.ok(h.run("scan.types.size") <= 65);
  assert.equal(h.run("[...scan.types.values()].reduce((s,v) => s+v.objects, 0n)"), 2003n);
});

test("failed first scan never presents zero and resets previous results", async () => {
  const h = harness();
  h.run("storageScope = {bucket: 'allowed', prefix: 'data/'}");
  await assert.rejects(h.run("scanPage(true)"), /Denied/);
  assert.match(h.elements.get("scan-status").textContent, /unknown, not zero/);
  assert.equal(h.elements.get("type-totals").children.length, 0);
});

test("failed continuation retains partial counts and retry token", async () => {
  const h = harness();
  h.run(`storageScope = {bucket: 'allowed', prefix: 'data/'};
    scan = {objects: 4n, bytes: 35n, markers: 0n, pages: 1, token: "next", types: new Map()};`);
  await assert.rejects(h.run("scanPage(false)"), /Denied/);
  assert.match(h.elements.get("scan-status").textContent, /PARTIAL ONLY: 4 objects, 35 bytes/);
  assert.equal(h.run("scan.token"), "next");
  assert.equal(h.elements.get("scan-next").hidden, false);
});

test("navigation resets totals and handles opaque encoded prefixes", async () => {
  const h = harness();
  h.run("storageScope = {bucket: 'allowed', prefix: 'old/'}; scan = {pages: 1};");
  await assert.rejects(h.run("openPrefix('allowed', 'a +/#?/')"), /Denied/);
  assert.equal(h.run("scan"), null);
  assert.match(h.elements.get("scan-status").textContent, /Totals unknown/);
  assert.equal(h.run("storageURL('/api/storage', 'a+b')"),
    "/api/storage?bucket=allowed&prefix=a+%2B%2F%23%3F%2F&token=a%2Bb");
});

test("scan makes one request per click, stays partial until last page, then completes", async () => {
  const h = harness();
  const calls = [];
  h.context.fetch = async (url) => {
    calls.push(url);
    const last = calls.length === 2;
    return {ok: true, json: async () => ({
      page_totals: {objects: 1, bytes: 42, folder_markers: 0,
        types: {".txt": {objects: 1, bytes: 42}}},
      is_truncated: !last, next_continuation_token: last ? null : "next",
      listed_at: "DEMO",
    })};
  };
  assert.equal(calls.length, 0);
  h.run("storageScope = {bucket: 'allowed', prefix: 'data/'}");
  await h.run("scanPage(true)");
  assert.equal(calls.length, 1);
  assert.match(h.elements.get("scan-status").textContent, /PARTIAL/);
  assert.equal(h.elements.get("scan-next").hidden, false);
  assert.doesNotMatch(h.elements.get("status").textContent, /Loading/);
  await h.run("scanPage(false)");
  assert.equal(calls.length, 2);
  assert.match(calls[1], /token=next/);
  assert.match(h.elements.get("scan-status").textContent, /COMPLETE.*2 objects, 84 bytes/);
  assert.equal(h.elements.get("scan-next").hidden, true);
});

test("non-advancing continuation cannot mark the scan complete or double count", async () => {
  const h = harness();
  h.run(`storageScope = {bucket: "allowed", prefix: ""};
    scan = {objects: 1n, bytes: 3n, markers: 0n, pages: 1, token: "same", types: new Map()};`);
  h.context.fetch = async () => ({ok: true, json: async () => ({
    is_truncated: true, next_continuation_token: "same",
  })});
  await assert.rejects(h.run("scanPage(false)"), /pagination did not advance/);
  assert.equal(h.run("scan.objects"), 1n);
  assert.match(h.elements.get("scan-status").textContent, /incomplete/);
});

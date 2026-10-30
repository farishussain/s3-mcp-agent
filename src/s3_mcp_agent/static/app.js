"use strict";

const byId = (id) => document.getElementById(id);
let nextToken = null;
let busy = false;
let storageScope = null;
let listingToken = null;
let scan = null;
const labels = {
  collected: "Collected", not_configured: "Not configured", access_denied: "Access denied",
  unsupported: "Unsupported", error: "Error",
};
const text = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

function card(title, value) {
  const article = document.createElement("article");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const body = document.createElement("pre");
  body.textContent = text(value);
  article.append(heading, body);
  return article;
}

async function request(path) {
  const response = await fetch(path, {headers: {"X-S3-Dashboard": "1"}, cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed.");
  return data;
}

async function run(action) {
  if (busy) return;
  busy = true;
  for (const element of document.querySelectorAll("button, input")) element.disabled = true;
  byId("status").textContent = "Loading from AWS…";
  try {
    await action();
  } catch (error) {
    byId("status").textContent = error.message || "Unable to load data.";
  } finally {
    busy = false;
    for (const element of document.querySelectorAll("button, input")) element.disabled = false;
  }
}

async function loadBuckets(token) {
  const data = await request("/api/buckets" + (token ? "?token=" + encodeURIComponent(token) : ""));
  byId("bucket-options").replaceChildren();
  for (const bucket of data.buckets) {
    const option = document.createElement("option");
    option.value = bucket.name;
    byId("bucket-options").append(option);
  }
  nextToken = data.next_continuation_token;
  byId("more").hidden = !nextToken;
  byId("status").textContent = `${data.buckets.length} buckets returned on this page. ${nextToken ? "More pages available." : "End of inventory."} Choose or enter a bucket to inspect.`;
}

function render(report) {
  byId("report-title").textContent = report.bucket;
  byId("timestamp").textContent = "Snapshot: " + report.generated_at;
  const missing = Object.entries(report.sources).filter(([, source]) =>
    !["collected", "not_configured"].includes(source.status));
  byId("coverage").textContent = missing.length
    ? `Incomplete evidence: ${missing.length} of 5 sources unavailable. Do not infer safety from missing data.`
    : "All five bucket-level sources were read or confirmed absent. Effective access is still not evaluated.";
  byId("sources").replaceChildren();
  for (const [name, source] of Object.entries(report.sources)) {
    const item = card(name.replaceAll("_", " "), labels[source.status] || source.status);
    if (source.message) {
      const message = document.createElement("p");
      message.textContent = `${source.error_code}: ${source.message}`;
      item.append(message);
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Observed data";
    const observed = document.createElement("pre");
    observed.textContent = source.observed === null ? "Unknown / unavailable" : text(source.observed);
    details.append(summary, observed);
    item.append(details);
    byId("sources").append(item);
  }
  byId("indicators").replaceChildren();
  for (const [name, value] of Object.entries(report.public_access_indicators)) {
    byId("indicators").append(card(name.replaceAll("_", " "), value === null ? "Unknown / unavailable" : value));
  }
  const policySource = report.sources.policy.status;
  byId("policy-status").textContent = `${labels[policySource] || policySource}. Parse status: ${report.bucket_policy.parse_status}.`;
  byId("policy-document").textContent = text(report.bucket_policy.document);
  byId("statements").replaceChildren();
  for (const [index, statement] of report.bucket_policy.statements.entries()) {
    byId("statements").append(card(`Statement ${index + 1}`, statement));
  }
  if (!report.bucket_policy.statements.length) {
    byId("statements").append(card("No statements to display", "Check source and parse status; unavailable policy data is not an empty policy."));
  }
  byId("acl-status").textContent = `ACL applicability: ${report.acl.applicability}. BucketOwnerEnforced disables ACLs. Unknown ownership means unknown applicability; other public-access controls may also affect grants.`;
  byId("grants").replaceChildren();
  for (const grant of report.acl.grants) byId("grants").append(card("Grant", grant));
  if (!report.acl.grants.length) byId("grants").append(card("No grants to display", "Check ACL source status before drawing conclusions."));
  byId("limitations").replaceChildren();
  for (const control of report.effective_access.not_evaluated_controls) {
    const li = document.createElement("li");
    li.textContent = control;
    byId("limitations").append(li);
  }
  byId("report").hidden = false;
}

byId("buckets").addEventListener("click", () => run(() => loadBuckets(null)));
byId("more").addEventListener("click", () => run(() => loadBuckets(nextToken)));
byId("inspect").addEventListener("click", () => run(async () => {
  byId("report").hidden = true;
  const bucket = byId("bucket").value.trim();
  if (!bucket) throw new Error("Enter or choose a bucket name first.");
  const report = await request("/api/permissions?bucket=" + encodeURIComponent(bucket));
  render(report);
  byId("status").textContent = "Snapshot loaded. Refresh explicitly to check for changes.";
}));

function resetScan() {
  scan = null;
  byId("scan-next").hidden = true;
  byId("scan-status").textContent = "Totals unknown. Start a scan to measure this prefix.";
  byId("type-totals").replaceChildren();
}

function storageURL(route, token) {
  const query = new URLSearchParams({bucket: storageScope.bucket, prefix: storageScope.prefix});
  if (token) query.set("token", token);
  return route + "?" + query;
}

function prefixButton(label, bucket, prefix) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", () => run(() => openPrefix(bucket, prefix)));
  return button;
}

async function openPrefix(bucket, prefix) {
  byId("storage").hidden = true;
  resetScan();
  storageScope = {bucket, prefix};
  listingToken = null;
  await loadStoragePage(null);
}

async function loadStoragePage(token) {
  const page = await request(storageURL("/api/storage", token));
  byId("storage-title").textContent = `Storage: ${storageScope.bucket} / ${storageScope.prefix || "(root)"}`;
  byId("breadcrumbs").replaceChildren(prefixButton("Bucket root", storageScope.bucket, ""));
  let prefix = "";
  const parts = storageScope.prefix.split("/");
  parts.pop();
  for (const part of parts) {
    prefix += part + "/";
    byId("breadcrumbs").append(prefixButton(part || "(empty segment)", storageScope.bucket, prefix));
  }
  byId("folders").replaceChildren();
  for (const child of page.prefixes) {
    byId("folders").append(prefixButton(`Prefix: ${child}`, storageScope.bucket, child));
  }
  byId("objects").replaceChildren();
  for (const item of page.objects) {
    const row = document.createElement("tr");
    for (const value of [item.key, item.size.toLocaleString(), item.inferred_type]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    byId("objects").append(row);
  }
  listingToken = page.next_continuation_token;
  byId("objects-next").hidden = !listingToken;
  byId("listing-status").textContent =
    `Immediate page only: ${page.page_totals.objects} objects, ${page.page_totals.bytes.toLocaleString()} bytes, ` +
    `${page.prefixes.length} child prefixes. ${page.is_truncated ? "More listing pages remain." : "End of immediate listing."} ` +
    `These are not recursive totals. Listed at ${page.listed_at}.`;
  byId("storage").hidden = false;
  byId("status").textContent = "Storage page loaded. Choose a prefix or start a recursive metadata scan.";
}

function mergeScanPage(state, page) {
  state.objects += BigInt(page.page_totals.objects);
  state.bytes += BigInt(page.page_totals.bytes);
  state.markers += BigInt(page.page_totals.folder_markers);
  for (const [type, totals] of Object.entries(page.page_totals.types)) {
    // Keep browser aggregation bounded even for adversarial extension cardinality.
    const group = state.types.has(type) || state.types.size < 64 ? type : "other extensions (grouped)";
    const current = state.types.get(group) || {objects: 0n, bytes: 0n};
    current.objects += BigInt(totals.objects);
    current.bytes += BigInt(totals.bytes);
    state.types.set(group, current);
  }
  state.pages += 1;
  state.token = page.next_continuation_token;
}

async function scanPage(restart) {
  if (restart) {
    resetScan();
    scan = {objects: 0n, bytes: 0n, markers: 0n, pages: 0, token: null, types: new Map()};
  }
  byId("scan-status").textContent = "Scanning one metadata page… Totals are not yet complete.";
  let page;
  try {
    page = await request(storageURL("/api/storage-scan", scan.token));
  } catch (error) {
    byId("scan-status").textContent = scan.pages
      ? `Scan failed. PARTIAL ONLY: ${scan.objects} objects, ${scan.bytes} bytes from ${scan.pages} successful pages. Retry the next page or restart.`
      : "Scan failed. Totals unknown, not zero. Retry or check AWS access.";
    byId("scan-next").hidden = !scan.pages;
    throw error;
  }
  if (page.is_truncated && (!page.next_continuation_token || page.next_continuation_token === scan.token)) {
    byId("scan-status").textContent = "Scan incomplete: pagination did not advance. Restart the scan; do not treat totals as complete.";
    byId("scan-next").hidden = true;
    throw new Error("S3 pagination did not advance.");
  }
  mergeScanPage(scan, page);
  byId("scan-next").hidden = !page.is_truncated;
  byId("scan-status").textContent =
    `${page.is_truncated ? "PARTIAL — more pages remain" : "COMPLETE traversal of current prefix"}: ` +
    `${scan.objects.toLocaleString()} objects, ${scan.bytes.toLocaleString()} bytes; ${scan.markers} folder markers included. ` +
    `${scan.pages} pages scanned recursively under ${storageScope.prefix || "(bucket root)"}. ` +
    `Latest page: ${page.listed_at}. Not an atomic snapshot or billed storage.`;
  byId("type-totals").replaceChildren();
  for (const [type, totals] of scan.types) {
    byId("type-totals").append(card(`Inferred: ${type}`, `${totals.objects.toLocaleString()} objects · ${totals.bytes.toLocaleString()} bytes`));
  }
  byId("status").textContent = page.is_truncated
    ? "One metadata page scanned. Continue explicitly to include more objects."
    : "Metadata traversal complete for this prefix. See scope and snapshot limitations below.";
}

byId("browse").addEventListener("click", () => run(async () => {
  const bucket = byId("bucket").value.trim();
  if (!bucket) throw new Error("Enter or choose a bucket name first.");
  await openPrefix(bucket, "");
}));
byId("objects-next").addEventListener("click", () => run(() => loadStoragePage(listingToken)));
byId("scan-start").addEventListener("click", () => run(() => scanPage(true)));
byId("scan-next").addEventListener("click", () => run(() => scanPage(false)));
byId("bucket").addEventListener("input", () => {
  byId("storage").hidden = true;
  byId("report").hidden = true;
  storageScope = null;
  resetScan();
});

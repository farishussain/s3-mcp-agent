"use strict";

const byId = (id) => document.getElementById(id);
let nextToken = null;
let busy = false;
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
  for (const id of ["buckets", "more", "inspect", "bucket"]) byId(id).disabled = true;
  byId("status").textContent = "Loading from AWS…";
  try {
    await action();
  } catch (error) {
    byId("status").textContent = error.message || "Unable to load data.";
  } finally {
    busy = false;
    for (const id of ["buckets", "more", "inspect", "bucket"]) byId(id).disabled = false;
  }
}

async function loadBuckets(token) {
  const data = await request("/api/buckets" + (token ? "?token=" + encodeURIComponent(token) : ""));
  if (!token) byId("bucket-options").replaceChildren();
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

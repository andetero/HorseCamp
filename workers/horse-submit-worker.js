/**
 * HorseCamp submission intake Worker.
 *
 * Deploy to Cloudflare Workers and set these secrets/vars:
 *   GITHUB_TOKEN   = fine-grained PAT or classic token with repo issues write access
 *   GITHUB_OWNER   = andetero
 *   GITHUB_REPO    = HorseCamp
 *
 * Optional:
 *   ALLOWED_ORIGIN = https://your-site.example.com
 *
 * Endpoint:
 *   POST /submissions
 */

const MAX_BODY_BYTES = 16 * 1024;

function jsonResponse(body, status = 200, origin = "*") {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": origin,
      "access-control-allow-methods": "POST, OPTIONS",
      "access-control-allow-headers": "content-type",
    },
  });
}

function cleanText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function normalizeKind(value) {
  const raw = cleanText(value).toLowerCase().replace(/[ -]+/g, "_");
  if (["layover", "horse_layover", "horse_layovers"].includes(raw)) return "layover";
  if (["private_camp", "private", "camp", "horse_camp", "horse_camping"].includes(raw)) return "private_camp";
  return "";
}

function normalizePhone(value) {
  const raw = cleanText(value);
  if (!raw) return "";
  const digits = raw.replace(/\D+/g, "");
  if (digits.length === 11 && digits.startsWith("1")) {
    return `+1 ${digits.slice(1, 4)}-${digits.slice(4, 7)}-${digits.slice(7)}`;
  }
  if (digits.length === 10) {
    return `${digits.slice(0, 3)}-${digits.slice(3, 6)}-${digits.slice(6)}`;
  }
  return raw;
}

function normalizeStringList(value) {
  if (Array.isArray(value)) {
    return [...new Set(value.map(cleanText).filter(Boolean))];
  }
  if (typeof value === "string") {
    return [...new Set(value.split(/[,;|]/).map(cleanText).filter(Boolean))];
  }
  return [];
}

function normalizeBool(value) {
  if (typeof value === "boolean") return value;
  return ["1", "true", "yes", "y", "on", "available"].includes(cleanText(value).toLowerCase());
}

function normalizeNumber(value, fallback = 0) {
  if (value === undefined || value === null || value === "") return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function validateAndNormalizeSubmission(input) {
  const type = normalizeKind(input.type);
  if (!type) throw new Error("type must be layover or private_camp");

  const name = cleanText(input.name);
  if (name.length < 3) throw new Error("name is required");

  const state = cleanText(input.state).toUpperCase();
  if (!/^[A-Z]{2}$/.test(state)) throw new Error("state must be a two-letter abbreviation");

  const latitude = Number(input.latitude);
  const longitude = Number(input.longitude);
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) throw new Error("latitude is invalid");
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) throw new Error("longitude is invalid");
  if (latitude === 0 && longitude === 0) throw new Error("latitude/longitude cannot both be zero");

  let website = cleanText(input.website);
  if (website && !/^https?:\/\//i.test(website)) website = `https://${website}`;

  const description = cleanText(input.description);
  const location = cleanText(input.location || input.address);

  return {
    type,
    name,
    state,
    latitude,
    longitude,
    location,
    phone: normalizePhone(input.phone),
    website,
    description,
    notes: cleanText(input.notes),
    hookups: normalizeStringList(input.hookups),
    accommodations: normalizeStringList(input.accommodations),
    maxRigLength: Math.max(0, Math.trunc(normalizeNumber(input.maxRigLength))),
    stallCount: Math.max(0, Math.trunc(normalizeNumber(input.stallCount))),
    paddockCount: Math.max(0, Math.trunc(normalizeNumber(input.paddockCount))),
    pricePerNight: Math.max(0, normalizeNumber(input.pricePerNight)),
    horseFeePerNight: Math.max(0, normalizeNumber(input.horseFeePerNight)),
    hasWashRack: normalizeBool(input.hasWashRack),
    hasDumpStation: normalizeBool(input.hasDumpStation),
    hasWifi: normalizeBool(input.hasWifi),
    hasBathhouse: normalizeBool(input.hasBathhouse),
    pullThroughAvailable: normalizeBool(input.pullThroughAvailable),
    submitterAppVersion: cleanText(input.submitterAppVersion),
    submittedAt: new Date().toISOString(),
  };
}

function markdownValue(value) {
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "—";
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isFinite(value) && value !== 0 ? String(value) : "—";
  const cleaned = cleanText(value);
  if (!cleaned) return "—";
  return cleaned.replace(/\|/g, "\\|");
}

function issueBody(submission) {
  const humanType = submission.type === "layover" ? "Layover" : "Private Camp";
  const rows = [
    ["Type", humanType],
    ["Name", submission.name],
    ["State", submission.state],
    ["Coordinates", `${submission.latitude}, ${submission.longitude}`],
    ["Location / Address", submission.location],
    ["Phone", submission.phone],
    ["Website", submission.website],
    ["Description", submission.description],
    ["Reviewer / submitter notes", submission.notes],
    ["Hookups", submission.hookups],
    ["Accommodations", submission.accommodations],
    ["Max rig length", submission.maxRigLength],
    ["Stall count", submission.stallCount],
    ["Paddock / corral count", submission.paddockCount],
    ["Nightly price", submission.pricePerNight],
    ["Horse fee", submission.horseFeePerNight],
    ["Wash rack", submission.hasWashRack],
    ["Dump station", submission.hasDumpStation],
    ["Wi-Fi", submission.hasWifi],
    ["Bathhouse", submission.hasBathhouse],
    ["Pull-through available", submission.pullThroughAvailable],
    ["Submitted from app version", submission.submitterAppVersion],
    ["Submitted at", submission.submittedAt],
  ];

  const detailTable = [
    "| Field | Value |",
    "| --- | --- |",
    ...rows.map(([label, value]) => `| ${label} | ${markdownValue(value)} |`),
  ];

  const lines = [
    `## ${humanType} submission`,
    "",
    ...detailTable,
    "",
    "### Approval",
    "",
    "Review the details. If it looks good, add the `approved` label. A GitHub Action will create a PR that updates the source JSON for final review before merge. The scheduled Seed Camp Data workflow will rebuild `camps.json` later, or you can run it manually.",
    "",
    "### Submission JSON",
    "",
    "<details>",
    "<summary>Raw JSON used by automation</summary>",
    "",
    "```json",
    JSON.stringify(submission, null, 2),
    "```",
    "</details>",
    "",
    "<!-- HORSECAMP_SUBMISSION_JSON",
    JSON.stringify(submission, null, 2),
    "HORSECAMP_SUBMISSION_JSON -->",
  ];
  return lines.join("\n");
}

async function createIssue(env, submission) {
  const labels = [
    "submission",
    "needs-review",
    submission.type === "layover" ? "layover" : "private-camp",
  ];

  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/issues`, {
    method: "POST",
    headers: {
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "accept": "application/vnd.github+json",
      "content-type": "application/json",
      "user-agent": "HorseCamp-Submission-Worker",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({
      title: `New HorseCamp submission: ${submission.name}`,
      body: issueBody(submission),
      labels,
    }),
  });

  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`GitHub issue creation failed: ${res.status} ${JSON.stringify(payload).slice(0, 500)}`);
  }
  return payload;
}

export default {
  async fetch(request, env) {
    const allowedOrigin = env.ALLOWED_ORIGIN || "*";
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return jsonResponse({ ok: true }, 200, allowedOrigin);
    }

    if (request.method !== "POST" || url.pathname !== "/submissions") {
      return jsonResponse({ ok: false, message: "Use POST /submissions" }, 404, allowedOrigin);
    }

    if (!env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) {
      return jsonResponse({ ok: false, message: "Worker is missing GitHub configuration" }, 500, allowedOrigin);
    }

    const contentLength = Number(request.headers.get("content-length") || "0");
    if (contentLength > MAX_BODY_BYTES) {
      return jsonResponse({ ok: false, message: "Submission is too large" }, 413, allowedOrigin);
    }

    let input;
    try {
      input = await request.json();
    } catch {
      return jsonResponse({ ok: false, message: "Invalid JSON" }, 400, allowedOrigin);
    }

    try {
      const submission = validateAndNormalizeSubmission(input);
      const issue = await createIssue(env, submission);
      return jsonResponse({
        ok: true,
        issueNumber: issue.number,
        issueUrl: issue.html_url,
        message: "Submission received for review.",
      }, 201, allowedOrigin);
    } catch (err) {
      return jsonResponse({ ok: false, message: err.message || "Submission failed" }, 400, allowedOrigin);
    }
  },
};

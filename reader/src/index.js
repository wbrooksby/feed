// Reader: fetches an article and returns just its text, like Safari Reader.
// Nothing is stored except Cloudflare's short-lived cache of the response.
import { parseHTML } from "linkedom";
import { Readability } from "@mozilla/readability";

const ALLOWED_ORIGINS = ["https://wbrooksby.github.io", "http://localhost:8765"];
const UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const CACHE_SECONDS = 6 * 3600;

const KEEP = new Set(["p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "i", "b",
  "a", "img", "figure", "figcaption", "br", "hr", "table", "thead", "tbody", "tr", "th", "td", "sup", "sub", "dl", "dt", "dd"]);
const DROP = new Set(["script", "style", "iframe", "object", "embed", "form", "input", "button", "svg", "noscript", "video", "audio", "source", "picture"]);
const ATTRS = { a: ["href"], img: ["src", "alt"], td: ["colspan"], th: ["colspan"] };

function cors(origin) {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
    "Vary": "Origin",
  };
}

function json(body, status, origin, extra = {}) {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json; charset=utf-8", ...cors(origin), ...extra },
  });
}

function absolute(url, base) {
  try {
    const u = new URL(url, base);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch { return null; }
}

// Keep only plain article markup: no scripts, styles, classes or event handlers.
function clean(node, doc, base) {
  for (const el of [...node.children]) {
    const tag = el.tagName.toLowerCase();
    if (DROP.has(tag)) { el.remove(); continue; }
    clean(el, doc, base);
    if (!KEEP.has(tag)) {
      el.replaceWith(...el.childNodes); // unwrap layout tags, keep their text
      continue;
    }
    for (const a of [...el.attributes]) {
      if (!(ATTRS[tag] || []).includes(a.name)) el.removeAttribute(a.name);
    }
    if (tag === "a") {
      const href = absolute(el.getAttribute("href") || "", base);
      href ? el.setAttribute("href", href) : el.removeAttribute("href");
    }
    if (tag === "img") {
      const src = absolute(el.getAttribute("src") || "", base);
      if (!src || src.startsWith("data:")) { el.remove(); continue; }
      el.setAttribute("src", src);
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") return new Response(null, { headers: { ...cors(origin), "Access-Control-Allow-Methods": "GET" } });
    // Only the feed app may use this, so it can't be used as an open proxy.
    if (!ALLOWED_ORIGINS.includes(origin)) return json({ error: "forbidden" }, 403, origin);

    const target = absolute(new URL(request.url).searchParams.get("url") || "");
    if (!target) return json({ error: "bad url" }, 400, origin);

    const cacheKey = new Request("https://reader.cache/?url=" + encodeURIComponent(target));
    const cached = await caches.default.match(cacheKey);
    if (cached) {
      const r = new Response(cached.body, cached);
      for (const [k, v] of Object.entries(cors(origin))) r.headers.set(k, v);
      return r;
    }

    let res;
    try {
      res = await fetch(target, {
        headers: { "User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en" },
        redirect: "follow", cf: { cacheTtl: 600 },
      });
    } catch (e) {
      return json({ error: "fetch failed" }, 502, origin);
    }
    if (!res.ok) return json({ error: `site returned ${res.status}` }, 502, origin);
    if (!(res.headers.get("content-type") || "").includes("html")) return json({ error: "not an article" }, 422, origin);

    const html = await res.text();
    const { document } = parseHTML(html);
    const article = new Readability(document, { charThreshold: 400 }).parse();
    if (!article || !article.content || (article.textContent || "").trim().length < 400) {
      return json({ error: "no article text (paywall or not an article)" }, 422, origin);
    }

    const { document: out } = parseHTML(`<!doctype html><html><body><div id="r">${article.content}</div></body></html>`);
    const root = out.getElementById("r");
    clean(root, out, res.url || target);
    const words = (article.textContent || "").trim().split(/\s+/).length;
    if ((root.textContent || "").trim().length < 300) {
      return json({ error: "no article text (paywall or not an article)" }, 422, origin);
    }

    const body = {
      url: res.url || target,
      title: article.title || "",
      byline: article.byline || "",
      site: article.siteName || "",
      published: article.publishedTime || "",
      minutes: Math.max(1, Math.round(words / 230)),
      html: root.innerHTML,
    };
    const response = json(body, 200, origin, { "Cache-Control": `public, max-age=${CACHE_SECONDS}` });
    ctx.waitUntil(caches.default.put(cacheKey, response.clone()));
    return response;
  },
};

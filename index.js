const express    = require("express");
const puppeteer  = require("puppeteer");
const Handlebars = require("handlebars");
const cors       = require("cors");
const fs         = require("fs");
const path       = require("path");

const app = express();
app.use(cors());
app.use(express.json({ limit: "10mb" }));

// ── Load constants from env ──────────────────────────────────
const LOGO_B64 = fs.existsSync(path.join(__dirname, "logo.png"))
  ? fs.readFileSync(path.join(__dirname, "logo.png")).toString("base64")
  : "";

// ── Parse SM list from env var (set SM_LIST on Railway) ─────
// Format: [{"id":"guneet","name":"...","title":"...","email":"..."}]
const SM_LIST = (() => {
  try { return JSON.parse(process.env.SM_LIST || "[]"); } catch { return []; }
})();

// ── Compile template once ────────────────────────────────────
const templateSrc = fs.readFileSync(path.join(__dirname, "template.html"), "utf8");
const template    = Handlebars.compile(templateSrc);

Handlebars.registerHelper("formatIDR", val => {
  const n = typeof val === "string" ? parseInt(val.replace(/\D/g, "")) : (val || 0);
  return "IDR " + n.toLocaleString("id-ID");
});

function formatDate(d) {
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}
function addDays(d, n) {
  const r = new Date(d); r.setDate(r.getDate() + n); return r;
}
function parseIDR(v) {
  return parseInt(String(v || "0").replace(/\D/g, "")) || 0;
}

// ── GET /sm-list ─────────────────────────────────────────────
app.get("/sm-list", (_, res) => {
  res.json(SM_LIST.map(({ id, name, title, email }) => ({ id, name, title, email })));
});

// ── POST /generate-pdf ───────────────────────────────────────
app.post("/generate-pdf", async (req, res) => {
  try {
    const data = { ...req.body };

    // Resolve SM from SM_LIST env var
    if (data.smId) {
      const sm = SM_LIST.find(s => s.id === data.smId);
      if (sm) {
        data.smName  = data.smName  || sm.name;
        data.smTitle = data.smTitle || sm.title;
        data.smEmail = data.smEmail || sm.email;
        // Auto-inject signature: SIG_GUNEET, SIG_PRASHANTH, etc.
        const sigKey = `SIG_${data.smId.toUpperCase()}`;
        if (process.env[sigKey] && !data.smSignatureBase64) {
          data.smSignatureBase64 = process.env[sigKey];
        }
      }
    }

    data.logoBase64    = LOGO_B64;
    data.quotationDate = formatDate(new Date());
    data.validUntil    = formatDate(addDays(new Date(), 30));

    // Enrich line items
    data.lineItems = (data.lineItems || []).map((item, i) => {
      const rate   = parseIDR(item.monthlyRate);
      const months = parseInt(item.durationMonths) || 1;
      return {
        ...item,
        no                  : i + 1,
        totalAmount         : rate * months,
        monthlyRateFormatted: "IDR " + rate.toLocaleString("id-ID"),
        totalFormatted      : "IDR " + (rate * months).toLocaleString("id-ID"),
        durationLabel       : `${months} Month${months > 1 ? "s" : ""}`,
      };
    });

    const grandTotal = data.lineItems.reduce((s, i) => s + i.totalAmount, 0);
    data.grandTotalFormatted = "IDR " + grandTotal.toLocaleString("id-ID");

    // Render → PDF
    const html    = template(data);
    const browser = await puppeteer.launch({
      headless: "new",
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    });
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: "domcontentloaded" });
    // Wait for auto-shrink JS
    await new Promise(r => setTimeout(r, 350));

    const pdfBuffer = await page.pdf({
      format         : "A4",
      printBackground: true,
      margin         : { top: "0.4in", bottom: "0.35in", left: "0.6in", right: "0.6in" },
    });
    await browser.close();

    res.set({
      "Content-Type"       : "application/pdf",
      "Content-Disposition": `attachment; filename="${data.quotationNumber}.pdf"`,
      "Content-Length"     : pdfBuffer.length,
    });
    res.send(pdfBuffer);

  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

app.get("/health", (_, res) => res.json({ status: "ok" }));

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`PDF service on :${PORT}`));
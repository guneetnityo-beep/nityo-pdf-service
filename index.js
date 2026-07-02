const express    = require("express");
const puppeteer  = require("puppeteer");
const Handlebars = require("handlebars");
const cors       = require("cors");
const fs         = require("fs");
const path       = require("path");

const app = express();
app.use(cors());
app.use(express.json({ limit: "10mb" }));

function formatDate(d) {
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}
function addDays(d, n) {
  const r = new Date(d); r.setDate(r.getDate() + n); return r;
}
function parseIDR(v) {
  return parseInt(String(v || "0").replace(/\D/g, "")) || 0;
}
function idr(n) {
  const abs = Math.abs(Math.round(n));
  return (n < 0 ? "- " : "") + "IDR " + abs.toLocaleString("id-ID");
}

// Register helpers
Handlebars.registerHelper("eq", (a, b) => a === b);

// Load + compile template at startup — Railway must redeploy if template changes
const templateSrc = fs.readFileSync(path.join(__dirname, "template.html"), "utf8");
const template    = Handlebars.compile(templateSrc);

// Load logo once
let logoBase64 = "";
try {
  const p = path.join(__dirname, "nityo-logo.png");
  if (fs.existsSync(p)) logoBase64 = fs.readFileSync(p).toString("base64");
} catch (_) {}

// Load signatures from disk — filename = email address + .png
// e.g. guneet.chhabra@nityo.com.png
const signatures = {};
try {
  fs.readdirSync(__dirname)
    .filter(f => f.endsWith(".png") && f.includes("@"))
    .forEach(f => {
      const email = f.replace(/\.png$/, "");
      signatures[email] = fs.readFileSync(path.join(__dirname, f)).toString("base64");
    });
  console.log("Signatures loaded:", Object.keys(signatures));
} catch (_) {}

app.post("/generate-pdf", async (req, res) => {
  try {
    const data = { ...req.body };

    // ── Flatten sender / manager ──────────────────────────
    data.smEmail          = (data.sender  && data.sender.email)           || "";
    data.smName           = (data.manager && data.manager.name)           || "";
    data.smTitle          = (data.manager && data.manager.title)          || "";
    data.smSignatureBase64 = signatures[data.smEmail] || (data.manager && data.manager.signatureBase64) || "";
    data.logoBase64       = logoBase64;
    data.quotationDate    = data.quotationDate || formatDate(new Date());
    data.validUntil       = data.validUntil    || formatDate(addDays(new Date(), 30));

    // ── Enrich line items ────────────────────────────────
    data.lineItems = (data.lineItems || []).map((item, i) => {
      const rate     = parseIDR(item.monthlyRate);
      const isDaily  = item.rateType === "daily";
      const months   = parseInt(item.durationMonths) || 1;
      const total    = isDaily ? rate : rate * months;
      return {
        ...item,
        no                  : i + 1,
        totalAmount         : total,
        monthlyRateFormatted: idr(rate),
        totalFormatted      : idr(total),
        durationLabel       : isDaily ? "Daily" : `${months} Month${months > 1 ? "s" : ""}`,
        rateLabel           : isDaily ? "Daily Rate (IDR)" : "Monthly Rate (IDR)",
      };
    });

    const lineSubtotal = data.lineItems.reduce((s, i) => s + i.totalAmount, 0);

    // ── Totals (keep subtotal and grand total separate) ─────────────
    const ppnAmount   = Math.round(lineSubtotal * 0.11);
    const grandTotal  = data.includePpn ? lineSubtotal + ppnAmount : lineSubtotal;

    data.subtotalFormatted     = idr(lineSubtotal);   // always excl. PPN
    data.ppnFormatted          = idr(ppnAmount);
    data.grandTotalFormatted   = idr(lineSubtotal);   // shown as subtotal row
    data.totalInclPpnFormatted = idr(grandTotal);     // shown as grand total row

    // ── Breakdown rows (SEPARATE from line items — informational only) ─
    // These do NOT affect grandTotal. They are their own standalone table.
    const bdRows = [];
    let bdRunning = 0;

    (data.breakdownRows || []).forEach(row => {
      if (!row.label) return;
      let amount = 0;
      if (row.kind === "add") {
        amount = parseIDR(row.value);
        bdRunning += amount;
        bdRows.push({ label: row.label, type: "add", formatted: idr(amount) });
      } else if (row.kind === "subtract") {
        amount = parseIDR(row.value);
        bdRunning -= amount;
        bdRows.push({ label: row.label, type: "subtract", formatted: "- " + idr(amount) });
      } else if (row.kind === "percent") {
        const pct = parseFloat(row.value) || 0;
        amount = Math.round(lineSubtotal * pct / 100);
        bdRunning += amount;
        bdRows.push({ label: `${row.label} (${pct}%)`, type: "add", formatted: idr(Math.abs(amount)) });
      }
    });

    if (bdRows.length > 0) {
      bdRows.push({ label: "Breakdown Total", type: "total", formatted: idr(bdRunning) });
    }

    data.bdRows        = bdRows;
    data.showBreakdown = !!data.showBreakdown && bdRows.length > 0;

    // ── Render ────────────────────────────────────────────
    const html    = template(data);
    const browser = await puppeteer.launch({
      headless: "new",
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    });
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: "networkidle0" });
    const pdfBuffer = await page.pdf({
      format         : "A4",
      printBackground: true,
      margin         : { top: "12mm", bottom: "18mm", left: "14mm", right: "14mm" },
    });
    await browser.close();

    res.set({
      "Content-Type"       : "application/pdf",
      "Content-Disposition": `attachment; filename="${data.quotationNumber || "quotation"}.pdf"`,
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

// ── Rate Card PDF endpoint ────────────────────────────────────
let rcTemplate = null;
try {
  const rcTemplateSrc = fs.readFileSync(path.join(__dirname, "rate-card-template.html"), "utf8");
  rcTemplate = Handlebars.compile(rcTemplateSrc);
  console.log("Rate card template loaded");
} catch(e) {
  console.warn("Rate card template not found:", e.message);
}

app.post("/generate-rate-card-pdf", async (req, res) => {
  if (!rcTemplate) return res.status(500).json({ error: "Rate card template not loaded — check rate-card-template.html is deployed" });
  try {
    const data = { ...req.body };

    data.smEmail           = (data.sender  && data.sender.email)            || "";
    data.smName            = (data.manager && data.manager.name)            || "";
    data.smTitle           = (data.manager && data.manager.title)           || "";
    data.smSignatureBase64 = signatures[data.smEmail] || (data.manager && data.manager.signatureBase64) || "";
    data.logoBase64        = logoBase64;

    // Group roles by level
    const levels = ["Junior","Mid","Senior"];
    data.grouped = levels.map(level => ({
      level,
      roles: (data.roles || []).filter(r => r.level === level).map(r => ({
        ...r,
        minRateFormatted: "IDR " + Math.round(r.minRate||0).toLocaleString("id-ID"),
        maxRateFormatted: "IDR " + Math.round(r.maxRate||0).toLocaleString("id-ID"),
        rateTypeLabel   : r.rateType === "daily" ? "Daily" : "Monthly",
      })),
    })).filter(g => g.roles.length > 0);

    const html    = rcTemplate(data);
    const browser = await puppeteer.launch({ headless:"new", args:["--no-sandbox","--disable-setuid-sandbox"] });
    const page    = await browser.newPage();
    await page.setContent(html, { waitUntil:"networkidle0" });
    const pdfBuffer = await page.pdf({ format:"A4", printBackground:true, margin:{ top:"12mm", bottom:"14mm", left:"14mm", right:"14mm" } });
    await browser.close();

    res.set({ "Content-Type":"application/pdf", "Content-Length":pdfBuffer.length });
    res.send(pdfBuffer);
  } catch(err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});